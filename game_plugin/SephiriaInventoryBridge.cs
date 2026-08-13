using BepInEx;
using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.IO.Pipes;
using System.Reflection;
using System.Runtime.Serialization;
using System.Runtime.Serialization.Json;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using UnityEngine;

namespace SephiriaInventoryBridge
{
    [BepInPlugin("local.sephiria.inventorybridge", "Sephiria Inventory Bridge", "1.2.2")]
    public sealed class InventoryBridgePlugin : BaseUnityPlugin
    {
        private const string PipeName = "SephiriaInventoryBridge.v1";
        private const string ApplyPipeName = "SephiriaInventoryBridge.apply.v1";
        private const int MaxCommandBytes = 1000000;
        private volatile string _snapshot = "{\"version\":1,\"ready\":false,\"error\":\"waiting for inventory\"}";
        private volatile bool _running;
        private Thread _pipeThread;
        private Thread _commandThread;
        private readonly object _pipeSync = new object();
        private readonly object _commandPipeSync = new object();
        private readonly object _commandQueueSync = new object();
        private readonly Queue<ApplyWork> _commandQueue = new Queue<ApplyWork>();
        private NamedPipeServerStream _activePipe;
        private NamedPipeServerStream _activeCommandPipe;
        private Type _gridInventoryType;
        private Type _itemPositionType;
        private Type _dungeonManagerType;
        private MethodInfo _swapMethod;
        private MethodInfo _clickMethod;
        private float _nextCapture;
        private string _assemblySha256 = "";
        private readonly Dictionary<string, string> _customCandidateCache = new Dictionary<string, string>();

        private void Awake()
        {
            _gridInventoryType = Type.GetType("GridInventory, Assembly-CSharp", false);
            _itemPositionType = Type.GetType("ItemPosition, Assembly-CSharp", false);
            _dungeonManagerType = Type.GetType("DungeonManager, Assembly-CSharp", false);
            if (_gridInventoryType != null && _itemPositionType != null)
            {
                _swapMethod = _gridInventoryType.GetMethod(
                    "Swap", BindingFlags.Public | BindingFlags.Instance, null,
                    new Type[] { typeof(sbyte), typeof(sbyte), typeof(sbyte), typeof(sbyte) }, null);
                _clickMethod = _gridInventoryType.GetMethod(
                    "DoClickAction", BindingFlags.Public | BindingFlags.Instance, null,
                    new Type[] { _itemPositionType }, null);
            }
            _assemblySha256 = ComputeAssemblyHash();
            _running = true;
            _pipeThread = new Thread(PipeLoop);
            _pipeThread.IsBackground = true;
            _pipeThread.Name = "SephiriaInventoryBridge";
            _pipeThread.Start();
            _commandThread = new Thread(CommandPipeLoop);
            _commandThread.IsBackground = true;
            _commandThread.Name = "SephiriaInventoryApplyBridge";
            _commandThread.Start();
            Logger.LogInfo("Inventory bridge started; apply API available: " +
                (_swapMethod != null && _clickMethod != null));
        }

        private void Update()
        {
            ProcessNextCommand();
            if (Time.unscaledTime < _nextCapture)
                return;
            _nextCapture = Time.unscaledTime + 0.5f;
            try
            {
                _snapshot = BuildSnapshot();
            }
            catch (Exception exception)
            {
                _snapshot = ErrorSnapshot("读取背包失败: " + exception.GetType().Name);
                Logger.LogWarning(exception);
            }
        }

        private void OnDestroy()
        {
            StopBridge();
        }

        private void OnApplicationQuit()
        {
            StopBridge();
        }

        private void StopBridge()
        {
            _running = false;
            lock (_commandQueueSync)
            {
                while (_commandQueue.Count > 0)
                {
                    ApplyWork work = _commandQueue.Dequeue();
                    work.Response = ApplyResponse.Failure("SHUTTING_DOWN", "游戏正在退出");
                    work.Completed.Set();
                }
            }
            // Never close or wait on a named pipe from Unity's shutdown thread. The
            // workers use cancellable asynchronous accepts and dispose their own pipes.
        }

        private bool WaitForConnection(NamedPipeServerStream pipe)
        {
            IAsyncResult pending = pipe.BeginWaitForConnection(null, null);
            while (_running)
            {
                if (pending.AsyncWaitHandle.WaitOne(100))
                {
                    pipe.EndWaitForConnection(pending);
                    return true;
                }
            }
            return false;
        }

        private void PipeLoop()
        {
            while (_running)
            {
                NamedPipeServerStream pipe = null;
                try
                {
                    pipe = new NamedPipeServerStream(
                        PipeName, PipeDirection.Out, 1, PipeTransmissionMode.Byte, PipeOptions.Asynchronous);
                    lock (_pipeSync)
                    {
                        if (!_running)
                            return;
                        _activePipe = pipe;
                    }
                    if (!WaitForConnection(pipe))
                        continue;
                    byte[] payload = Encoding.UTF8.GetBytes(_snapshot);
                    pipe.Write(payload, 0, payload.Length);
                    pipe.Flush();
                }
                catch (Exception exception)
                {
                    if (_running)
                    {
                        Logger.LogWarning("Inventory pipe error: " + exception.Message);
                        Thread.Sleep(250);
                    }
                }
                finally
                {
                    lock (_pipeSync)
                    {
                        if (System.Object.ReferenceEquals(_activePipe, pipe))
                            _activePipe = null;
                    }
                    if (pipe != null)
                    {
                        try { pipe.Dispose(); }
                        catch { }
                    }
                }
            }
        }

        private void CommandPipeLoop()
        {
            while (_running)
            {
                NamedPipeServerStream pipe = null;
                try
                {
                    pipe = new NamedPipeServerStream(
                        ApplyPipeName, PipeDirection.InOut, 1,
                        PipeTransmissionMode.Byte, PipeOptions.Asynchronous);
                    lock (_commandPipeSync)
                    {
                        if (!_running)
                            return;
                        _activeCommandPipe = pipe;
                    }
                    if (!WaitForConnection(pipe))
                        continue;
                    int length = ReadInt32(pipe);
                    if (length <= 0 || length > MaxCommandBytes)
                        throw new InvalidDataException("Invalid apply command length");
                    ApplyCommand command;
                    using (MemoryStream input = new MemoryStream(ReadExactly(pipe, length)))
                    {
                        DataContractJsonSerializer serializer =
                            new DataContractJsonSerializer(typeof(ApplyCommand));
                        command = serializer.ReadObject(input) as ApplyCommand;
                    }
                    ApplyWork work = new ApplyWork(command);
                    lock (_commandQueueSync)
                        _commandQueue.Enqueue(work);
                    if (!work.Completed.WaitOne(15000))
                    {
                        work.Cancelled = true;
                        work.Response = ApplyResponse.Failure(
                            "APPLY_TIMEOUT", "等待游戏主线程应用排布超时");
                    }
                    WriteResponse(pipe, work.Response ?? ApplyResponse.Failure(
                        "APPLY_FAILED", "游戏没有返回应用结果"));
                }
                catch (Exception exception)
                {
                    if (_running)
                        Logger.LogWarning("Apply pipe error: " + exception.Message);
                }
                finally
                {
                    lock (_commandPipeSync)
                    {
                        if (System.Object.ReferenceEquals(_activeCommandPipe, pipe))
                            _activeCommandPipe = null;
                    }
                    if (pipe != null)
                    {
                        try { pipe.Dispose(); }
                        catch { }
                    }
                }
            }
        }

        private static int ReadInt32(Stream stream)
        {
            byte[] bytes = ReadExactly(stream, 4);
            return bytes[0] | bytes[1] << 8 | bytes[2] << 16 | bytes[3] << 24;
        }

        private static byte[] ReadExactly(Stream stream, int length)
        {
            byte[] result = new byte[length];
            int offset = 0;
            while (offset < length)
            {
                int read = stream.Read(result, offset, length - offset);
                if (read <= 0)
                    throw new EndOfStreamException();
                offset += read;
            }
            return result;
        }

        private static void WriteResponse(Stream stream, ApplyResponse response)
        {
            byte[] payload;
            using (MemoryStream output = new MemoryStream())
            {
                DataContractJsonSerializer serializer =
                    new DataContractJsonSerializer(typeof(ApplyResponse));
                serializer.WriteObject(output, response);
                payload = output.ToArray();
            }
            byte[] length = BitConverter.GetBytes(payload.Length);
            stream.Write(length, 0, length.Length);
            stream.Write(payload, 0, payload.Length);
            stream.Flush();
        }

        private void ProcessNextCommand()
        {
            ApplyWork work = null;
            lock (_commandQueueSync)
            {
                if (_commandQueue.Count > 0)
                    work = _commandQueue.Dequeue();
            }
            if (work == null)
                return;
            if (work.Cancelled)
            {
                work.Completed.Set();
                return;
            }
            try
            {
                work.Response = ExecuteApply(work.Command);
            }
            catch (Exception exception)
            {
                Logger.LogWarning("Apply command failed: " + exception);
                work.Response = ApplyResponse.Failure(
                    "APPLY_FAILED", "应用背包排布失败: " + exception.GetType().Name);
            }
            finally
            {
                work.Completed.Set();
            }
        }

        private ApplyResponse ExecuteApply(ApplyCommand command)
        {
            if (command == null || command.version != 1 || command.operation != "apply")
                return ApplyResponse.Failure("INVALID_COMMAND", "应用命令版本或操作无效");
            if (_swapMethod == null || _clickMethod == null || _itemPositionType == null)
                return ApplyResponse.Failure("UNSUPPORTED_GAME_VERSION", "当前游戏版本缺少受支持的背包移动接口");
            if (!String.Equals(command.assemblySha256, _assemblySha256, StringComparison.OrdinalIgnoreCase))
                return ApplyResponse.Failure("GAME_VERSION_CHANGED", "游戏版本与读取背包时不一致，请重新读取");
            object inventory = FindLocalInventory();
            if (inventory == null)
                return ApplyResponse.Failure("INVENTORY_UNAVAILABLE", "尚未找到本地玩家背包");
            if (!GetBool(inventory, "isServer"))
                return ApplyResponse.Failure("HOST_REQUIRED", "自动应用仅支持单机或主机背包");
            int cellCount = GetInt(inventory, "CurrentInventoryStorage", 0);
            int width = GetInt(inventory, "Width", 6);
            if (command.cellCount != cellCount || width != 6)
                return ApplyResponse.Failure("INVENTORY_CHANGED", "背包格数已变化，请重新读取并求解");
            List<LiveItem> before;
            string validationError;
            if (!TryCaptureAndValidateTargets(inventory, command.placements, cellCount,
                                              out before, out validationError))
                return ApplyResponse.Failure("INVALID_APPLY_PLAN", validationError);
            List<ApplyPlacement> originals = new List<ApplyPlacement>();
            foreach (LiveItem item in before)
                originals.Add(new ApplyPlacement {
                    instanceId = item.InstanceId, kind = item.Kind, cell = item.Cell,
                    rotation = item.Kind == "tablet" ? item.Rotation : -1,
                });

            int moves = 0;
            int rotations = 0;
            try
            {
                ApplyTargets(inventory, command.placements, cellCount, ref moves, ref rotations);
                string finalFingerprint = ComputeInventoryFingerprint(inventory);
                _snapshot = BuildSnapshot();
                return ApplyResponse.Success(finalFingerprint, moves, rotations);
            }
            catch (Exception exception)
            {
                bool rolledBack = false;
                try
                {
                    int rollbackMoves = 0;
                    int rollbackRotations = 0;
                    ApplyTargets(inventory, originals.ToArray(), cellCount,
                                 ref rollbackMoves, ref rollbackRotations);
                    rolledBack = true;
                    _snapshot = BuildSnapshot();
                }
                catch (Exception rollbackException)
                {
                    Logger.LogError("Inventory rollback failed: " + rollbackException);
                }
                ApplyResponse failure = ApplyResponse.Failure(
                    "APPLY_FAILED", "游戏未能完成排布，" +
                    (rolledBack ? "已恢复原排布" : "自动恢复失败，请立即检查背包"));
                failure.rolledBack = rolledBack;
                Logger.LogWarning(exception);
                return failure;
            }
        }

        private void ApplyTargets(object inventory, ApplyPlacement[] targets, int cellCount,
                                  ref int moves, ref int rotations)
        {
            foreach (ApplyPlacement target in targets)
            {
                Dictionary<int, LiveItem> current = CaptureInventoryItems(inventory);
                LiveItem item;
                if (!current.TryGetValue(target.instanceId, out item))
                    throw new InvalidOperationException("Item disappeared while applying arrangement");
                if (item.Cell == target.cell)
                    continue;
                int targetX = target.cell % 6;
                int targetY = target.cell / 6;
                _swapMethod.Invoke(inventory, new object[] {
                    (sbyte)item.X, (sbyte)item.Y, (sbyte)targetX, (sbyte)targetY,
                });
                Dictionary<int, LiveItem> changed = CaptureInventoryItems(inventory);
                LiveItem moved;
                if (!changed.TryGetValue(target.instanceId, out moved) || moved.Cell != target.cell)
                    throw new InvalidOperationException("Game rejected inventory swap");
                moves++;
            }

            foreach (ApplyPlacement target in targets)
            {
                if (target.kind != "tablet")
                    continue;
                int attempts = 0;
                while (true)
                {
                    Dictionary<int, LiveItem> current = CaptureInventoryItems(inventory);
                    LiveItem item;
                    if (!current.TryGetValue(target.instanceId, out item) || item.Kind != "tablet")
                        throw new InvalidOperationException("Tablet disappeared while applying rotation");
                    if (item.Rotation == target.rotation)
                        break;
                    if (attempts++ >= 4 || !GetEffectiveRotatable(item.Tablet, item.InstanceId))
                        throw new InvalidOperationException("Tablet cannot reach requested rotation");
                    object position = Activator.CreateInstance(
                        _itemPositionType, new object[] { (sbyte)item.X, (sbyte)item.Y });
                    _clickMethod.Invoke(inventory, new object[] { position });
                    Dictionary<int, LiveItem> changed = CaptureInventoryItems(inventory);
                    LiveItem rotated;
                    if (!changed.TryGetValue(target.instanceId, out rotated) ||
                        rotated.Rotation == item.Rotation)
                        throw new InvalidOperationException("Game rejected tablet rotation");
                    rotations++;
                }
            }

            Dictionary<int, LiveItem> final = CaptureInventoryItems(inventory);
            foreach (ApplyPlacement target in targets)
            {
                LiveItem item;
                if (!final.TryGetValue(target.instanceId, out item) || item.Cell != target.cell ||
                    (target.kind == "tablet" && item.Rotation != target.rotation))
                    throw new InvalidOperationException("Final inventory verification failed");
            }
        }

        private bool TryCaptureAndValidateTargets(
            object inventory, ApplyPlacement[] targets, int cellCount,
            out List<LiveItem> before, out string error)
        {
            before = new List<LiveItem>(CaptureInventoryItems(inventory).Values);
            before.Sort(delegate(LiveItem left, LiveItem right) {
                return left.InstanceId.CompareTo(right.InstanceId);
            });
            error = "";
            if (targets == null || targets.Length == 0 || targets.Length != before.Count)
            {
                error = "应用方案没有包含当前背包中的全部物品";
                return false;
            }
            Dictionary<int, LiveItem> live = new Dictionary<int, LiveItem>();
            foreach (LiveItem item in before)
            {
                if (item.Kind == "other" || live.ContainsKey(item.InstanceId))
                {
                    error = "当前背包包含不支持或重复的物品实例";
                    return false;
                }
                live[item.InstanceId] = item;
            }
            HashSet<int> ids = new HashSet<int>();
            HashSet<int> cells = new HashSet<int>();
            foreach (ApplyPlacement target in targets)
            {
                LiveItem item;
                if (target == null || target.instanceId <= 0 ||
                    !live.TryGetValue(target.instanceId, out item) || !ids.Add(target.instanceId) ||
                    target.kind != item.Kind || target.cell < 0 || target.cell >= cellCount ||
                    !cells.Add(target.cell) ||
                    (target.kind == "tablet" && (target.rotation < 0 || target.rotation > 3)) ||
                    (target.kind == "artifact" && target.rotation != -1))
                {
                    error = "应用方案中的实例、位置或旋转无效";
                    return false;
                }
            }
            return ids.Count == live.Count;
        }

        private Dictionary<int, LiveItem> CaptureInventoryItems(object inventory)
        {
            Dictionary<int, LiveItem> result = new Dictionary<int, LiveItem>();
            IEnumerable entries = GetMember(inventory, "inventoryMatrix") as IEnumerable;
            if (entries == null)
                return result;
            foreach (object entry in entries)
            {
                object position = GetMember(entry, "Key");
                object item = GetMember(entry, "Value");
                if (item == null)
                    continue;
                object charm = GetMember(item, "Charm");
                object tablet = GetMember(item, "StoneTablet");
                int instanceId = GetInt(item, "InstanceID", 0);
                LiveItem live = new LiveItem();
                live.InstanceId = instanceId;
                live.EntityId = GetInt(item, "EntityID", 0);
                live.X = GetInt(position, "x", GetInt(item, "XIdx", -1));
                live.Y = GetInt(position, "y", GetInt(item, "YIdx", -1));
                live.Cell = live.Y * 6 + live.X;
                live.Kind = charm != null ? "artifact" : (tablet != null ? "tablet" : "other");
                live.Tablet = tablet;
                live.Rotation = tablet == null ? -1 : GetInt(tablet, "rotation", 0);
                if (result.ContainsKey(instanceId))
                    throw new InvalidOperationException("Duplicate item instance ID");
                result[instanceId] = live;
            }
            return result;
        }

        private string ComputeInventoryFingerprint(object inventory)
        {
            List<LiveItem> items = new List<LiveItem>(CaptureInventoryItems(inventory).Values);
            items.Sort(delegate(LiveItem left, LiveItem right) {
                int compare = left.InstanceId.CompareTo(right.InstanceId);
                return compare != 0 ? compare : String.CompareOrdinal(left.Kind, right.Kind);
            });
            UnityEngine.Object unityInventory = inventory as UnityEngine.Object;
            StringBuilder canonical = new StringBuilder();
            canonical.Append(_assemblySha256).Append('|')
                .Append(unityInventory == null ? 0 : unityInventory.GetInstanceID()).Append('|')
                .Append(GetInt(inventory, "Width", 6)).Append('|')
                .Append(GetInt(inventory, "CurrentInventoryStorage", 0));
            foreach (LiveItem item in items)
            {
                canonical.Append('|').Append(item.Kind).Append(':')
                    .Append(item.InstanceId).Append(':').Append(item.EntityId).Append(':')
                    .Append(item.X).Append(':').Append(item.Y).Append(':').Append(item.Rotation);
            }
            using (SHA256 sha = SHA256.Create())
                return BitConverter.ToString(sha.ComputeHash(
                    Encoding.UTF8.GetBytes(canonical.ToString()))).Replace("-", "").ToLowerInvariant();
        }

        private string BuildSnapshot()
        {
            if (_gridInventoryType == null)
                return ErrorSnapshot("当前游戏版本缺少 GridInventory 类型");
            object inventory = FindLocalInventory();
            if (inventory == null)
                return ErrorSnapshot("尚未找到本地玩家背包");

            int width = GetInt(inventory, "Width", 6);
            int height = GetInt(inventory, "Height", 0);
            int cellCount = GetInt(inventory, "CurrentInventoryStorage", 0);
            if (cellCount <= 0)
                cellCount = width * height;

            StringBuilder artifacts = new StringBuilder();
            StringBuilder tablets = new StringBuilder();
            bool firstArtifact = true;
            bool firstTablet = true;
            object matrix = GetMember(inventory, "inventoryMatrix");
            IEnumerable entries = matrix as IEnumerable;
            if (entries != null)
            {
                foreach (object entry in entries)
                {
                    object position = GetMember(entry, "Key");
                    object item = GetMember(entry, "Value");
                    if (item == null)
                        continue;
                    int x = GetInt(position, "x", GetInt(item, "XIdx", -1));
                    int y = GetInt(position, "y", GetInt(item, "YIdx", -1));
                    int entityId = GetInt(item, "EntityID", 0);
                    int instanceId = GetInt(item, "InstanceID", 0);
                    string name = GetString(item, "Name");
                    object charm = GetMember(item, "Charm");
                    object tablet = GetMember(item, "StoneTablet");
                    if (charm != null)
                    {
                        if (!firstArtifact) artifacts.Append(',');
                        firstArtifact = false;
                        artifacts.Append('{');
                        JsonNumber(artifacts, "entityId", entityId, true);
                        JsonNumber(artifacts, "instanceId", instanceId, false);
                        JsonNumber(artifacts, "x", x, false);
                        JsonNumber(artifacts, "y", y, false);
                        JsonString(artifacts, "name", name, false);
                        JsonNumber(artifacts, "displayedLevel", GetInt(charm, "DisplayedLevel", 0), false);
                        JsonNumber(artifacts, "effectEnabledLevel", GetInt(charm, "EffectEnabledLevel", 0), false);
                        int temporaryLevel = GetMatrixValue(inventory, "dungeonTempLevels", x, y);
                        JsonNumber(artifacts, "enchantLevel", GetEnchantLevel(instanceId, temporaryLevel), false);
                        JsonNumber(artifacts, "temporaryLevel", temporaryLevel, false);
                        JsonNumber(artifacts, "gridLevel", GetMatrixValue(inventory, "levelMatrix", x, y), false);
                        artifacts.Append('}');
                    }
                    else if (tablet != null)
                    {
                        bool isCustom = GetBool(tablet, "isCustomTablet");
                        bool rotatable = GetEffectiveRotatable(tablet, instanceId);
                        string query = GetEffectiveTabletString(tablet, "GetQuery", "query", instanceId);
                        string conditionQuery = GetEffectiveTabletString(
                            tablet, "GetConditionQuery", "conditionQuery", instanceId);
                        string[] queryRotations = GetRotatedQueries(tablet, query, rotatable);
                        string[] conditionRotations = GetRotatedQueries(tablet, conditionQuery, rotatable);
                        if (!firstTablet) tablets.Append(',');
                        firstTablet = false;
                        tablets.Append('{');
                        JsonNumber(tablets, "entityId", entityId, true);
                        JsonNumber(tablets, "instanceId", instanceId, false);
                        JsonNumber(tablets, "x", x, false);
                        JsonNumber(tablets, "y", y, false);
                        JsonNumber(tablets, "rotation", GetInt(tablet, "rotation", 0), false);
                        JsonString(tablets, "name", name, false);
                        JsonBoolean(tablets, "isCustom", isCustom, false);
                        JsonBoolean(tablets, "rotatable", rotatable, false);
                        JsonString(tablets, "query", query, false);
                        JsonString(tablets, "conditionQuery", conditionQuery, false);
                        JsonStringArray(tablets, "queryRotations", queryRotations, false);
                        JsonStringArray(tablets, "conditionRotations", conditionRotations, false);
                        if (isCustom)
                        {
                            tablets.Append(",\"customCandidates\":").Append(
                                CompileCustomCandidates(tablet, query, conditionQuery, rotatable,
                                                        width, height, cellCount));
                        }
                        tablets.Append('}');
                    }
                }
            }

            StringBuilder json = new StringBuilder(2048 + artifacts.Length + tablets.Length);
            json.Append('{');
            JsonNumber(json, "version", 2, true);
            JsonBoolean(json, "ready", true, false);
            JsonNumber(json, "width", width, false);
            JsonNumber(json, "height", height, false);
            JsonNumber(json, "cellCount", cellCount, false);
            JsonString(json, "assemblySha256", _assemblySha256, false);
            JsonString(json, "capturedAt", DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture), false);
            JsonString(json, "inventoryFingerprint", ComputeInventoryFingerprint(inventory), false);
            json.Append(",\"artifacts\":[").Append(artifacts).Append(']');
            json.Append(",\"tablets\":[").Append(tablets).Append(']');
            json.Append('}');
            return json.ToString();
        }

        private string GetEffectiveTabletString(object tablet, string methodName, string fieldName, int instanceId)
        {
            object value = InvokeMember(tablet, methodName, new object[] { instanceId });
            if (value == null)
                return GetString(tablet, fieldName);
            return Convert.ToString(value, CultureInfo.InvariantCulture) ?? "";
        }

        private bool GetEffectiveRotatable(object tablet, int instanceId)
        {
            bool fallback = GetBool(tablet, "isRotatable");
            if (_dungeonManagerType == null)
                return fallback;
            try
            {
                MethodInfo method = _dungeonManagerType.GetMethod(
                    "IsTabletRotatable", BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static,
                    null, new Type[] { typeof(int), typeof(bool) }, null);
                if (method == null)
                    return fallback;
                object value = method.Invoke(null, new object[] { instanceId, fallback });
                return value is bool ? (bool)value : fallback;
            }
            catch { return fallback; }
        }

        private static string[] GetRotatedQueries(object tablet, string query, bool rotatable)
        {
            int count = rotatable ? 4 : 1;
            string[] values = new string[count];
            for (int rotation = 0; rotation < count; rotation++)
            {
                object value = InvokeMember(tablet, "GetRotatedQuery", new object[] { query ?? "", rotation });
                values[rotation] = value == null
                    ? (query ?? "")
                    : (Convert.ToString(value, CultureInfo.InvariantCulture) ?? "");
            }
            return values;
        }

        private int GetEnchantLevel(int instanceId, int fallback)
        {
            if (_dungeonManagerType == null || instanceId <= 0)
                return fallback;
            try
            {
                PropertyInfo property = _dungeonManagerType.GetProperty(
                    "Instance", BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static);
                object manager = property == null ? null : property.GetValue(null, null);
                object value = InvokeMember(
                    manager, "GetGlobalItemStatValue", new object[] { instanceId, "Enchant" });
                string text = value as string;
                if (!String.IsNullOrEmpty(text))
                    return ToInt(text, fallback);
            }
            catch { }
            return fallback;
        }

        private string CompileCustomCandidates(
            object tablet, string query, string conditionQuery, bool rotatable,
            int width, int height, int cellCount)
        {
            string cacheKey = width.ToString(CultureInfo.InvariantCulture) + ":" +
                height.ToString(CultureInfo.InvariantCulture) + ":" +
                cellCount.ToString(CultureInfo.InvariantCulture) + ":" +
                (rotatable ? "1" : "0") + ":" + query + "\n--CONDITION--\n" + conditionQuery;
            string cached;
            if (_customCandidateCache.TryGetValue(cacheKey, out cached))
                return cached;
            if (_itemPositionType == null)
                throw new InvalidOperationException("ItemPosition type is unavailable");

            StringBuilder output = new StringBuilder();
            output.Append('[');
            bool first = true;
            int rotationCount = rotatable ? 4 : 1;
            for (int cell = 0; cell < cellCount; cell++)
            {
                int x = cell % width;
                int y = cell / width;
                object origin = Activator.CreateInstance(
                    _itemPositionType, new object[] { (sbyte)x, (sbyte)y });
                for (int rotation = 0; rotation < rotationCount; rotation++)
                {
                    if (!first) output.Append(',');
                    first = false;
                    output.Append('[').Append(cell.ToString(CultureInfo.InvariantCulture)).Append(',')
                        .Append(rotation.ToString(CultureInfo.InvariantCulture)).Append(',')
                        .Append(ParseMetadata(tablet, query, width, height, cellCount, origin, rotation)).Append(',')
                        .Append(ParseMetadata(tablet, conditionQuery, width, height, cellCount, origin, rotation))
                        .Append(']');
                }
            }
            output.Append(']');
            cached = output.ToString();
            if (_customCandidateCache.Count > 32)
                _customCandidateCache.Clear();
            _customCandidateCache[cacheKey] = cached;
            return cached;
        }

        private static string ParseMetadata(
            object tablet, string query, int width, int height, int cellCount,
            object origin, int rotation)
        {
            object[] arguments = new object[] {
                query ?? "", width, height, cellCount, origin, rotation, null,
            };
            object parsed = InvokeMember(tablet, "ParseQuery", arguments);
            IEnumerable entries = parsed as IEnumerable;
            StringBuilder output = new StringBuilder();
            output.Append('[');
            bool first = true;
            if (entries != null)
            {
                foreach (object entry in entries)
                {
                    object position = GetMember(entry, "position");
                    int x = GetInt(position, "x", -1);
                    int y = GetInt(position, "y", -1);
                    int cell = y * width + x;
                    if (x < 0 || x >= width || y < 0 || y >= height || cell < 0 || cell >= cellCount)
                        continue;
                    if (!first) output.Append(',');
                    first = false;
                    output.Append('[').Append(cell.ToString(CultureInfo.InvariantCulture)).Append(',');
                    AppendQuoted(output, GetString(entry, "value"));
                    output.Append(']');
                }
            }
            output.Append(']');
            return output.ToString();
        }

        private static object InvokeMember(object target, string name, object[] arguments)
        {
            if (target == null) return null;
            Type type = target.GetType();
            while (type != null)
            {
                foreach (MethodInfo method in type.GetMethods(
                    BindingFlags.Public | BindingFlags.NonPublic |
                    BindingFlags.Instance | BindingFlags.Static))
                {
                    if (method.Name != name || method.GetParameters().Length != arguments.Length)
                        continue;
                    try { return method.Invoke(target, arguments); }
                    catch { return null; }
                }
                type = type.BaseType;
            }
            return null;
        }

        private object FindLocalInventory()
        {
            UnityEngine.Object[] candidates = UnityEngine.Object.FindObjectsOfType(_gridInventoryType);
            object best = null;
            int bestScore = Int32.MinValue;
            foreach (UnityEngine.Object candidate in candidates)
            {
                object avatar = GetMember(candidate, "UnitAvatar");
                int score = 0;
                if (avatar != null) score += 100;
                if (GetBool(candidate, "isLocalPlayer") || GetBool(avatar, "isLocalPlayer")) score += 10000;
                if (GetBool(candidate, "authority") || GetBool(avatar, "authority")) score += 1000;
                score += Math.Min(99, GetCollectionCount(GetMember(candidate, "inventoryMatrix")));
                if (score > bestScore)
                {
                    bestScore = score;
                    best = candidate;
                }
            }
            return best;
        }

        private static int GetMatrixValue(object inventory, string matrixName, int x, int y)
        {
            IEnumerable entries = GetMember(inventory, matrixName) as IEnumerable;
            if (entries == null) return 0;
            foreach (object entry in entries)
            {
                object position = GetMember(entry, "Key");
                if (GetInt(position, "x", -99) == x && GetInt(position, "y", -99) == y)
                    return GetInt(entry, "Value", 0);
            }
            return 0;
        }

        private static int GetCollectionCount(object value)
        {
            if (value == null) return 0;
            object count = GetMember(value, "Count");
            return ToInt(count, 0);
        }

        private static object GetMember(object target, string name)
        {
            if (target == null) return null;
            Type type = target.GetType();
            while (type != null)
            {
                FieldInfo field = type.GetField(name, BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
                if (field != null)
                {
                    try { return field.GetValue(target); } catch { return null; }
                }
                PropertyInfo property = type.GetProperty(name, BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
                if (property != null && property.GetIndexParameters().Length == 0)
                {
                    try { return property.GetValue(target, null); } catch { return null; }
                }
                type = type.BaseType;
            }
            return null;
        }

        private static int GetInt(object target, string name, int fallback)
        {
            return ToInt(GetMember(target, name), fallback);
        }

        private static int ToInt(object value, int fallback)
        {
            if (value == null) return fallback;
            try { return Convert.ToInt32(value, CultureInfo.InvariantCulture); }
            catch { return fallback; }
        }

        private static bool GetBool(object target, string name)
        {
            object value = GetMember(target, name);
            return value is bool && (bool)value;
        }

        private static string GetString(object target, string name)
        {
            object value = GetMember(target, name);
            return value == null ? "" : Convert.ToString(value, CultureInfo.InvariantCulture);
        }

        private string ComputeAssemblyHash()
        {
            try
            {
                string path = Path.Combine(Application.dataPath, "Managed", "Assembly-CSharp.dll");
                using (FileStream stream = File.OpenRead(path))
                using (SHA256 sha = SHA256.Create())
                    return BitConverter.ToString(sha.ComputeHash(stream)).Replace("-", "").ToLowerInvariant();
            }
            catch (Exception exception)
            {
                Logger.LogWarning("Could not hash Assembly-CSharp.dll: " + exception.Message);
                return "";
            }
        }

        private string ErrorSnapshot(string message)
        {
            StringBuilder json = new StringBuilder();
            json.Append('{');
            JsonNumber(json, "version", 1, true);
            JsonBoolean(json, "ready", false, false);
            JsonString(json, "error", message, false);
            JsonString(json, "assemblySha256", _assemblySha256, false);
            JsonString(json, "capturedAt", DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture), false);
            json.Append('}');
            return json.ToString();
        }

        private static void JsonNumber(StringBuilder json, string name, int value, bool first)
        {
            if (!first) json.Append(',');
            json.Append('"').Append(name).Append("\":").Append(value.ToString(CultureInfo.InvariantCulture));
        }

        private static void JsonBoolean(StringBuilder json, string name, bool value, bool first)
        {
            if (!first) json.Append(',');
            json.Append('"').Append(name).Append("\":").Append(value ? "true" : "false");
        }

        private static void JsonString(StringBuilder json, string name, string value, bool first)
        {
            if (!first) json.Append(',');
            json.Append('"').Append(name).Append("\":");
            AppendQuoted(json, value);
        }

        private static void JsonStringArray(StringBuilder json, string name, string[] values, bool first)
        {
            if (!first) json.Append(',');
            json.Append('"').Append(name).Append("\":[");
            for (int index = 0; index < values.Length; index++)
            {
                if (index > 0) json.Append(',');
                AppendQuoted(json, values[index]);
            }
            json.Append(']');
        }

        private static void AppendQuoted(StringBuilder json, string value)
        {
            json.Append('"').Append(Escape(value)).Append('"');
        }

        private static string Escape(string value)
        {
            if (String.IsNullOrEmpty(value)) return "";
            StringBuilder escaped = new StringBuilder(value.Length + 8);
            foreach (char character in value)
            {
                switch (character)
                {
                    case '\\': escaped.Append("\\\\"); break;
                    case '"': escaped.Append("\\\""); break;
                    case '\n': escaped.Append("\\n"); break;
                    case '\r': escaped.Append("\\r"); break;
                    case '\t': escaped.Append("\\t"); break;
                    default:
                        if (character < 32)
                            escaped.Append("\\u").Append(((int)character).ToString("x4"));
                        else
                            escaped.Append(character);
                        break;
                }
            }
            return escaped.ToString();
        }

        private sealed class LiveItem
        {
            public int InstanceId;
            public int EntityId;
            public int X;
            public int Y;
            public int Cell;
            public int Rotation;
            public string Kind;
            public object Tablet;
        }

        private sealed class ApplyWork
        {
            public readonly ApplyCommand Command;
            public readonly ManualResetEvent Completed = new ManualResetEvent(false);
            public ApplyResponse Response;
            public volatile bool Cancelled;

            public ApplyWork(ApplyCommand command)
            {
                Command = command;
            }
        }

        [DataContract]
        private sealed class ApplyCommand
        {
            [DataMember] public int version;
            [DataMember] public string operation;
            [DataMember] public string assemblySha256;
            [DataMember] public int cellCount;
            [DataMember] public ApplyPlacement[] placements;
        }

        [DataContract]
        private sealed class ApplyPlacement
        {
            [DataMember] public int instanceId;
            [DataMember] public string kind;
            [DataMember] public int cell;
            [DataMember] public int rotation;
        }

        [DataContract]
        private sealed class ApplyResponse
        {
            [DataMember] public bool ok;
            [DataMember] public string code;
            [DataMember] public string message;
            [DataMember] public string inventoryFingerprint;
            [DataMember] public int moves;
            [DataMember] public int rotations;
            [DataMember] public bool rolledBack;

            public static ApplyResponse Failure(string code, string message)
            {
                return new ApplyResponse {
                    ok = false, code = code, message = message, inventoryFingerprint = "",
                    moves = 0, rotations = 0, rolledBack = false,
                };
            }

            public static ApplyResponse Success(string fingerprint, int moves, int rotations)
            {
                return new ApplyResponse {
                    ok = true, code = "APPLIED", message = "背包排布已应用并验证",
                    inventoryFingerprint = fingerprint, moves = moves,
                    rotations = rotations, rolledBack = false,
                };
            }
        }
    }
}
