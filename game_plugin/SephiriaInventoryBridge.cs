using BepInEx;
using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.IO.Pipes;
using System.Net;
using System.Reflection;
using System.Runtime.Serialization;
using System.Runtime.Serialization.Json;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using UnityEngine;
using UnityEngine.UI;

namespace SephiriaInventoryBridge
{
    [BepInPlugin("local.sephiria.inventorybridge", "Sephiria Inventory Bridge", "1.4.0")]
    public sealed class InventoryBridgePlugin : BaseUnityPlugin
    {
        private const string PipeName = "SephiriaInventoryBridge.v1";
        private const string ApplyPipeName = "SephiriaInventoryBridge.apply.v1";
        private const int MaxCommandBytes = 1000000;
        private const int AutoOrganizeRequestTimeoutMs = 120000;
        private const string AutoOrganizeRequestBody =
            "{\"fastMode\":true,\"timeLimitMs\":30000,\"workerCount\":0}";
        private volatile string _snapshot = "{\"version\":1,\"ready\":false,\"error\":\"waiting for inventory\"}";
        private volatile bool _running;
        private int _autoOrganizeRunning;
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
        private Type _uiManagerType;
        private Type _characterStatusPanelType;
        private Type _itemBoxPanelType;
        private PropertyInfo _uiManagerInstanceProperty;
        private MethodInfo _uiManagerGetElementMethod;
        private PropertyInfo _uiBaseIsOpenedProperty;
        private MethodInfo _swapMethod;
        private MethodInfo _clickMethod;
        private float _nextCapture;
        private float _nextBackpackUiPoll;
        private int _lastBackpackUiOpen = -1;
        private volatile bool _autoOrganizeUiRestorePending;
        private string _assemblySha256 = "";
        private Type _tmpTextType;
        private PropertyInfo _tmpTextProperty;
        private GameObject _organizeButtonObject;
        private Button _organizeButton;
        private Component _organizeButtonLabel;
        private bool _organizeUiCreated;
        private bool _backpackUiAnchorsLogged;
        private readonly Dictionary<string, string> _customCandidateCache = new Dictionary<string, string>();

        private void Awake()
        {
            _gridInventoryType = Type.GetType("GridInventory, Assembly-CSharp", false);
            _itemPositionType = Type.GetType("ItemPosition, Assembly-CSharp", false);
            _dungeonManagerType = Type.GetType("DungeonManager, Assembly-CSharp", false);
            ResolveUiReflection();
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
            Logger.LogInfo("Backpack UI detection ready: UIManager="
                + (_uiManagerType != null)
                + ", CharacterStatusPanel="
                + (_characterStatusPanelType != null)
                + ", ItemBoxPanel="
                + (_itemBoxPanelType != null));
            Logger.LogInfo("Packsmith auto-organize button: parent=inventoryScrollParent, layout=inventoryZone 右缘");
        }

        private void Update()
        {
            ProcessNextCommand();
            PollBackpackUiState();
            UpdateOrganizeButtonUi();
            ProcessAutoOrganizeUiRestore();
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
            DestroyOrganizeButtonUi();
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

        private void ResolveUiReflection()
        {
            _uiManagerType = Type.GetType("UIManager, Assembly-CSharp", false);
            _characterStatusPanelType = Type.GetType("UI_CharacterStatusPanel, Assembly-CSharp", false);
            _itemBoxPanelType = Type.GetType("UI_ItemBoxPanel, Assembly-CSharp", false);
            if (_uiManagerType != null)
            {
                _uiManagerInstanceProperty = _uiManagerType.GetProperty(
                    "Instance", BindingFlags.Public | BindingFlags.Static);
                MethodInfo[] methods = _uiManagerType.GetMethods(BindingFlags.Public | BindingFlags.Instance);
                for (int i = 0; i < methods.Length; i++)
                {
                    MethodInfo method = methods[i];
                    if (method.Name == "GetElement" && method.IsGenericMethodDefinition)
                    {
                        _uiManagerGetElementMethod = method;
                        break;
                    }
                }
            }
            Type uiBaseType = Type.GetType("UIBase, Assembly-CSharp", false);
            if (uiBaseType != null)
            {
                _uiBaseIsOpenedProperty = uiBaseType.GetProperty(
                    "IsOpened", BindingFlags.Public | BindingFlags.Instance);
            }
            _tmpTextType = Type.GetType("TMPro.TextMeshProUGUI, Unity.TextMeshPro", false);
            if (_tmpTextType != null)
                _tmpTextProperty = _tmpTextType.GetProperty("text", BindingFlags.Public | BindingFlags.Instance);
        }

        private object TryGetUiPanel(Type panelType)
        {
            if (panelType == null || _uiManagerType == null || _uiManagerInstanceProperty == null
                    || _uiManagerGetElementMethod == null)
                return null;
            object uiManager = _uiManagerInstanceProperty.GetValue(null, null);
            if (uiManager == null)
                return null;
            MethodInfo getElement = _uiManagerGetElementMethod.MakeGenericMethod(panelType);
            return getElement.Invoke(uiManager, null);
        }

        private bool IsBackpackUiOpen()
        {
            bool statusPanelOpen;
            if (TryGetUiPanelOpened(_characterStatusPanelType, out statusPanelOpen) && statusPanelOpen)
                return true;
            bool itemBoxOpen;
            if (TryGetUiPanelOpened(_itemBoxPanelType, out itemBoxOpen) && itemBoxOpen)
                return true;
            return false;
        }

        private bool TryGetUiPanelOpened(Type panelType, out bool isOpened)
        {
            isOpened = false;
            if (panelType == null || _uiManagerType == null || _uiManagerInstanceProperty == null
                    || _uiManagerGetElementMethod == null || _uiBaseIsOpenedProperty == null)
                return false;
            object uiManager = _uiManagerInstanceProperty.GetValue(null, null);
            if (uiManager == null)
                return false;
            MethodInfo getElement = _uiManagerGetElementMethod.MakeGenericMethod(panelType);
            object panel = getElement.Invoke(uiManager, null);
            if (panel == null)
                return false;
            object value = _uiBaseIsOpenedProperty.GetValue(panel, null);
            if (!(value is bool))
                return false;
            isOpened = (bool)value;
            return true;
        }

        private bool EvaluateInventoryHeuristic(out int matrixCount)
        {
            matrixCount = 0;
            object inventory = FindLocalInventory();
            if (inventory == null)
                return false;
            matrixCount = GetCollectionCount(GetMember(inventory, "inventoryMatrix"));
            return matrixCount > 0;
        }

        private void PollBackpackUiState()
        {
            if (Time.unscaledTime < _nextBackpackUiPoll)
                return;
            _nextBackpackUiPoll = Time.unscaledTime + 0.25f;

            bool uiOpen = IsBackpackUiOpen();
            int matrixCount;
            bool heuristicOpen = EvaluateInventoryHeuristic(out matrixCount);
            int current = uiOpen ? 1 : 0;
            if (_lastBackpackUiOpen == current)
                return;

            Logger.LogInfo(string.Format(CultureInfo.InvariantCulture,
                "背包 UI: {0} → {1} | CharacterStatusPanel={2}, ItemBoxPanel={3}, heuristic(inventory+matrix>0)={4}, matrixCount={5}",
                DescribeBackpackUiState(_lastBackpackUiOpen),
                DescribeBackpackUiState(current),
                DescribePanelState(_characterStatusPanelType),
                DescribePanelState(_itemBoxPanelType),
                heuristicOpen ? "yes" : "no",
                matrixCount));
            _lastBackpackUiOpen = current;
        }

        private static string DescribeBackpackUiState(int state)
        {
            if (state < 0)
                return "unknown";
            return state == 1 ? "open" : "closed";
        }

        private string DescribePanelState(Type panelType)
        {
            bool isOpened;
            if (!TryGetUiPanelOpened(panelType, out isOpened))
                return "?";
            return isOpened ? "open" : "closed";
        }

        private bool TryStartAutoOrganize()
        {
            if (Interlocked.CompareExchange(ref _autoOrganizeRunning, 1, 0) != 0)
            {
                Logger.LogInfo("Packsmith 自动整理正在进行中，请稍候");
                return false;
            }
            SetOrganizeButtonBusy(true);
            Thread worker = new Thread(AutoOrganizeWorker);
            worker.IsBackground = true;
            worker.Name = "PacksmithAutoOrganize";
            worker.Start();
            return true;
        }

        private void AutoOrganizeWorker()
        {
            try
            {
                Logger.LogInfo("Packsmith 自动整理已开始");
                string summary = RunAutoOrganizeRequest();
                Logger.LogInfo(summary);
            }
            catch (Exception exception)
            {
                Logger.LogWarning("Packsmith 自动整理失败: " + exception.Message);
            }
            finally
            {
                Interlocked.Exchange(ref _autoOrganizeRunning, 0);
                _autoOrganizeUiRestorePending = true;
            }
        }

        private void ProcessAutoOrganizeUiRestore()
        {
            if (!_autoOrganizeUiRestorePending || _autoOrganizeRunning != 0)
                return;
            _autoOrganizeUiRestorePending = false;
            SetOrganizeButtonBusy(false);
        }

        private void UpdateOrganizeButtonUi()
        {
            bool statusPanelOpen;
            TryGetUiPanelOpened(_characterStatusPanelType, out statusPanelOpen);
            if (!statusPanelOpen)
            {
                if (_organizeButtonObject != null)
                    _organizeButtonObject.SetActive(false);
                return;
            }

            if (_organizeButtonObject == null && !TryCreateOrganizeButtonUi())
                return;
            if (_organizeButtonObject == null)
                return;

            ApplyOrganizeButtonToBackpackSide();

            if (!_organizeButtonObject.activeSelf)
                _organizeButtonObject.SetActive(true);
            if (_autoOrganizeRunning != 0)
                SyncOrganizeButtonBusyState(true);
        }

        private bool TryGetCharacterStatusRectTransform(string fieldName, out RectTransform rectTransform)
        {
            rectTransform = null;
            object panel = TryGetUiPanel(_characterStatusPanelType);
            if (panel == null || _characterStatusPanelType == null)
                return false;
            FieldInfo field = _characterStatusPanelType.GetField(
                fieldName, BindingFlags.Public | BindingFlags.Instance);
            if (field == null)
                return false;
            rectTransform = field.GetValue(panel) as RectTransform;
            return rectTransform != null;
        }

        private Image TryGetCharacterStatusCoverImage()
        {
            RectTransform cover;
            if (!TryGetCharacterStatusRectTransform("inventoryCover", out cover) || cover == null)
                return null;
            return cover.GetComponent<Image>();
        }

        private bool TryGetOrganizeButtonAnchorTargets(
            out RectTransform scrollParent, out RectTransform inventoryZone)
        {
            scrollParent = null;
            inventoryZone = null;
            if (!TryGetCharacterStatusRectTransform("inventoryScrollParent", out scrollParent))
                return false;
            return TryGetCharacterStatusRectTransform("inventoryZone", out inventoryZone);
        }

        private Image TryGetOrganizeButtonBackgroundImage(object panel)
        {
            Type setEffectElementType = Type.GetType("UI_SetEffectElement, Assembly-CSharp", false);
            if (setEffectElementType != null && _characterStatusPanelType != null)
            {
                FieldInfo prefabField = _characterStatusPanelType.GetField(
                    "setEffectElementPrefab", BindingFlags.Public | BindingFlags.Instance);
                Component prefab = prefabField == null ? null : prefabField.GetValue(panel) as Component;
                if (prefab != null)
                {
                    FieldInfo backgroundField = setEffectElementType.GetField(
                        "iconBGImage", BindingFlags.Public | BindingFlags.Instance);
                    if (backgroundField != null)
                    {
                        Image background = backgroundField.GetValue(prefab) as Image;
                        if (background != null && background.sprite != null)
                            return background;
                    }
                }
            }

            Image coverImage = TryGetCharacterStatusCoverImage();
            if (coverImage != null && coverImage.sprite != null)
                return coverImage;

            RectTransform scrollParent;
            RectTransform inventoryZone;
            if (!TryGetOrganizeButtonAnchorTargets(out scrollParent, out inventoryZone) || scrollParent == null)
                return null;

            Image[] images = scrollParent.GetComponentsInChildren<Image>(true);
            for (int i = 0; i < images.Length; i++)
            {
                Image candidate = images[i];
                if (candidate == null || candidate.sprite == null)
                    continue;
                Transform candidateTransform = candidate.transform;
                if (inventoryZone != null && candidateTransform.IsChildOf(inventoryZone))
                    continue;
                return candidate;
            }
            return null;
        }

        private void ApplyOrganizeButtonBackground(Image buttonImage, object panel)
        {
            if (buttonImage == null)
                return;

            Image sourceImage = TryGetOrganizeButtonBackgroundImage(panel);
            if (sourceImage != null && sourceImage.sprite != null)
            {
                buttonImage.sprite = sourceImage.sprite;
                buttonImage.type = sourceImage.type == Image.Type.Filled
                    ? Image.Type.Simple
                    : sourceImage.type;
                buttonImage.pixelsPerUnitMultiplier = sourceImage.pixelsPerUnitMultiplier;
                buttonImage.material = sourceImage.material;
                buttonImage.color = Color.white;
                return;
            }

            buttonImage.sprite = null;
            buttonImage.type = Image.Type.Simple;
            buttonImage.color = new Color(0.45f, 0.34f, 0.24f, 0.92f);
        }

        private void ApplyOrganizeButtonLayout(RectTransform buttonRect)
        {
            if (buttonRect == null)
                return;

            RectTransform scrollParent;
            RectTransform inventoryZone;
            if (!TryGetOrganizeButtonAnchorTargets(out scrollParent, out inventoryZone))
                return;

            if (buttonRect.parent != scrollParent)
                buttonRect.SetParent(scrollParent, false);

            buttonRect.anchorMin = inventoryZone.anchorMin;
            buttonRect.anchorMax = inventoryZone.anchorMax;
            buttonRect.pivot = new Vector2(0f, 0.5f);
            buttonRect.sizeDelta = new Vector2(52f, 26f);
            buttonRect.anchoredPosition = new Vector2(
                inventoryZone.anchoredPosition.x + inventoryZone.sizeDelta.x * 0.5f + 18f,
                inventoryZone.anchoredPosition.y - 8f);
            buttonRect.SetAsLastSibling();
        }

        private void LogBackpackUiAnchorsOnce()
        {
            if (_backpackUiAnchorsLogged)
                return;
            object panel = TryGetUiPanel(_characterStatusPanelType);
            if (panel == null)
                return;
            _backpackUiAnchorsLogged = true;

            Logger.LogInfo("Packsmith 背包 UI 锚点确认（UI_CharacterStatusPanel 公开字段）");
            LogCharacterStatusRectTransform(panel, "inventoryParent");
            LogCharacterStatusRectTransform(panel, "inventoryScrollParent");
            LogCharacterStatusRectTransform(panel, "inventoryZone");
            LogCharacterStatusRectTransform(panel, "inventoryCover");
            LogCharacterStatusRectTransform(panel, "itemDropPositionOnController");

            RectTransform scrollParent;
            RectTransform inventoryZone;
            if (TryGetOrganizeButtonAnchorTargets(out scrollParent, out inventoryZone))
            {
                Logger.LogInfo(string.Format(CultureInfo.InvariantCulture,
                    "Packsmith 整理按钮布局: parent={0}, zoneRight={1:F1}, zoneCenterY={2:F1}（inventoryParent 为零宽竖条，不可作定位父级）",
                    GetTransformPath(scrollParent),
                    inventoryZone.anchoredPosition.x + inventoryZone.sizeDelta.x * 0.5f,
                    inventoryZone.anchoredPosition.y));
            }

            Image background = TryGetOrganizeButtonBackgroundImage(panel);
            Logger.LogInfo("Packsmith 整理按钮背景: "
                + (background != null && background.sprite != null
                    ? GetTransformPath(background.transform) + "/" + background.sprite.name
                    : "fallback"));

            FieldInfo dropZoneField = _characterStatusPanelType.GetField(
                "itemDropZone", BindingFlags.Public | BindingFlags.Instance);
            if (dropZoneField == null)
            {
                Logger.LogInfo("背包 UI.itemDropZone=字段缺失");
                return;
            }
            Component dropZone = dropZoneField.GetValue(panel) as Component;
            if (dropZone == null)
            {
                Logger.LogInfo("背包 UI.itemDropZone=null（右侧丢弃区，非整理按钮挂载点）");
                return;
            }
            Logger.LogInfo("背包 UI.itemDropZone: name="
                + dropZone.name
                + ", path="
                + GetTransformPath(dropZone.transform)
                + "（删除/丢弃区，勿复用其图标）");
        }

        private void LogCharacterStatusRectTransform(object panel, string fieldName)
        {
            FieldInfo field = _characterStatusPanelType.GetField(
                fieldName, BindingFlags.Public | BindingFlags.Instance);
            if (field == null)
            {
                Logger.LogInfo("背包 UI." + fieldName + "=字段缺失");
                return;
            }
            RectTransform rectTransform = field.GetValue(panel) as RectTransform;
            if (rectTransform == null)
            {
                Logger.LogInfo("背包 UI." + fieldName + "=null");
                return;
            }
            Logger.LogInfo(string.Format(CultureInfo.InvariantCulture,
                "背包 UI.{0}: name={1}, path={2}, anchor=({3:F3},{4:F3})-({5:F3},{6:F3}), pivot=({7:F3},{8:F3}), size=({9:F1},{10:F1}), pos=({11:F1},{12:F1}), active={13}",
                fieldName,
                rectTransform.name,
                GetTransformPath(rectTransform),
                rectTransform.anchorMin.x,
                rectTransform.anchorMin.y,
                rectTransform.anchorMax.x,
                rectTransform.anchorMax.y,
                rectTransform.pivot.x,
                rectTransform.pivot.y,
                rectTransform.sizeDelta.x,
                rectTransform.sizeDelta.y,
                rectTransform.anchoredPosition.x,
                rectTransform.anchoredPosition.y,
                rectTransform.gameObject.activeInHierarchy));
        }

        private static string GetTransformPath(Transform transform)
        {
            if (transform == null)
                return "";
            StringBuilder path = new StringBuilder(transform.name);
            Transform current = transform.parent;
            while (current != null)
            {
                path.Insert(0, current.name + "/");
                current = current.parent;
            }
            return path.ToString();
        }

        private Component FindReferenceTmpText(object panel)
        {
            Component panelComponent = panel as Component;
            if (panelComponent == null || _tmpTextType == null)
                return null;
            Component[] labels = panelComponent.GetComponentsInChildren(_tmpTextType, true);
            for (int i = 0; i < labels.Length; i++)
            {
                if (labels[i] != null)
                    return labels[i];
            }
            return null;
        }

        private void CopyTmpFontSettings(Component source, Component target)
        {
            if (source == null || target == null || _tmpTextType == null)
                return;
            string[] propertyNames = new string[] {
                "font", "fontSharedMaterial", "fontSize", "fontStyle", "color",
            };
            for (int i = 0; i < propertyNames.Length; i++)
            {
                PropertyInfo property = _tmpTextType.GetProperty(
                    propertyNames[i], BindingFlags.Public | BindingFlags.Instance);
                if (property == null || !property.CanRead || !property.CanWrite)
                    continue;
                property.SetValue(target, property.GetValue(source, null), null);
            }
        }

        private bool TryCreateOrganizeButtonUi()
        {
            if (_organizeUiCreated)
                return _organizeButtonObject != null;

            RectTransform scrollParent;
            RectTransform inventoryZone;
            if (!TryGetOrganizeButtonAnchorTargets(out scrollParent, out inventoryZone))
                return false;

            object panel = TryGetUiPanel(_characterStatusPanelType);
            if (panel == null)
                return false;

            LogBackpackUiAnchorsOnce();
            _organizeUiCreated = true;

            _organizeButtonObject = new GameObject(
                "PacksmithAutoOrganizeButton", typeof(RectTransform), typeof(CanvasRenderer), typeof(Image), typeof(Button));
            _organizeButtonObject.transform.SetParent(scrollParent, false);

            RectTransform buttonRect = _organizeButtonObject.transform as RectTransform;
            ApplyOrganizeButtonLayout(buttonRect);

            Image buttonImage = _organizeButtonObject.GetComponent<Image>();
            ApplyOrganizeButtonBackground(buttonImage, panel);
            buttonImage.raycastTarget = true;

            _organizeButton = _organizeButtonObject.GetComponent<Button>();
            _organizeButton.targetGraphic = buttonImage;
            ColorBlock colors = _organizeButton.colors;
            colors.normalColor = Color.white;
            colors.highlightedColor = new Color(0.85f, 0.85f, 0.85f, 1f);
            colors.pressedColor = new Color(0.70f, 0.70f, 0.70f, 1f);
            colors.disabledColor = new Color(0.55f, 0.55f, 0.55f, 0.65f);
            _organizeButton.colors = colors;
            _organizeButton.onClick = new Button.ButtonClickedEvent();
            _organizeButton.onClick.AddListener(OnOrganizeButtonClicked);

            if (_tmpTextType != null && _tmpTextProperty != null)
            {
                GameObject labelObject = new GameObject("Label", typeof(RectTransform));
                labelObject.transform.SetParent(_organizeButtonObject.transform, false);
                RectTransform labelRect = labelObject.transform as RectTransform;
                labelRect.anchorMin = Vector2.zero;
                labelRect.anchorMax = Vector2.one;
                labelRect.offsetMin = Vector2.zero;
                labelRect.offsetMax = Vector2.zero;

                _organizeButtonLabel = labelObject.AddComponent(_tmpTextType) as Component;
                Component referenceLabel = FindReferenceTmpText(panel);
                if (referenceLabel != null)
                    CopyTmpFontSettings(referenceLabel, _organizeButtonLabel);
                PropertyInfo fontSizeProperty = _tmpTextType.GetProperty(
                    "fontSize", BindingFlags.Public | BindingFlags.Instance);
                if (fontSizeProperty != null)
                    fontSizeProperty.SetValue(_organizeButtonLabel, 15f, null);
                PropertyInfo alignmentProperty = _tmpTextType.GetProperty(
                    "alignment", BindingFlags.Public | BindingFlags.Instance);
                if (alignmentProperty != null)
                    alignmentProperty.SetValue(_organizeButtonLabel, 514, null); // TextAlignmentOptions.Center
                PropertyInfo colorProperty = _tmpTextType.GetProperty(
                    "color", BindingFlags.Public | BindingFlags.Instance);
                if (colorProperty != null)
                    colorProperty.SetValue(_organizeButtonLabel, new Color(0.95f, 0.90f, 0.78f, 1f), null);
                PropertyInfo raycastProperty = _tmpTextType.GetProperty(
                    "raycastTarget", BindingFlags.Public | BindingFlags.Instance);
                if (raycastProperty != null)
                    raycastProperty.SetValue(_organizeButtonLabel, false, null);
            }

            SetOrganizeButtonLabel("整理");
            _organizeButtonObject.SetActive(false);
            Logger.LogInfo("Packsmith 整理按钮已创建: parent="
                + GetTransformPath(scrollParent)
                + ", layout=inventoryZone 右缘 + (18,-8)");
            return true;
        }

        private void ApplyOrganizeButtonToBackpackSide()
        {
            if (_organizeButtonObject == null)
                return;

            RectTransform buttonRect = _organizeButtonObject.transform as RectTransform;
            if (buttonRect == null)
                return;

            ApplyOrganizeButtonLayout(buttonRect);
        }

        private void OnOrganizeButtonClicked()
        {
            TryStartAutoOrganize();
        }

        private void SyncOrganizeButtonBusyState(bool busy)
        {
            if (_organizeButtonObject == null || !_organizeButtonObject.activeSelf)
                return;
            SetOrganizeButtonBusy(busy);
        }

        private void SetOrganizeButtonLabel(string label)
        {
            if (_organizeButtonLabel == null || _tmpTextProperty == null)
                return;
            _tmpTextProperty.SetValue(_organizeButtonLabel, label, null);
        }

        private void SetOrganizeButtonBusy(bool busy)
        {
            if (_organizeButton == null)
                return;
            _organizeButton.interactable = !busy;
            SetOrganizeButtonLabel(busy ? "…" : "整理");
        }

        private void DestroyOrganizeButtonUi()
        {
            if (_organizeButtonObject != null)
            {
                UnityEngine.Object.Destroy(_organizeButtonObject);
                _organizeButtonObject = null;
            }
            _organizeButton = null;
            _organizeButtonLabel = null;
            _organizeUiCreated = false;
        }

        private static string RuntimeInfoPath()
        {
            string localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
            return Path.Combine(localAppData, "SephiriaPacksmith", "runtime.json");
        }

        private static RuntimeInfo ReadRuntimeInfo(string path)
        {
            byte[] bytes = File.ReadAllBytes(path);
            using (MemoryStream stream = new MemoryStream(bytes))
            {
                DataContractJsonSerializer serializer =
                    new DataContractJsonSerializer(typeof(RuntimeInfo));
                object value = serializer.ReadObject(stream);
                RuntimeInfo runtime = value as RuntimeInfo;
                if (runtime == null)
                    throw new InvalidDataException("runtime.json 格式无效");
                return runtime;
            }
        }

        private string RunAutoOrganizeRequest()
        {
            string path = RuntimeInfoPath();
            if (!File.Exists(path))
                throw new IOException("未找到 runtime.json；请先启动 Packsmith 求解器");
            RuntimeInfo runtime = ReadRuntimeInfo(path);
            if (runtime.port <= 0 || string.IsNullOrEmpty(runtime.token))
                throw new InvalidDataException("runtime.json 缺少有效的 port 或 token");

            string url = "http://127.0.0.1:" + runtime.port.ToString(CultureInfo.InvariantCulture)
                + "/api/auto-organize";
            HttpWebRequest request = (HttpWebRequest)WebRequest.Create(url);
            request.Method = "POST";
            request.ContentType = "application/json";
            request.Headers["X-Sephiria-Token"] = runtime.token;
            request.Timeout = AutoOrganizeRequestTimeoutMs;
            request.ReadWriteTimeout = AutoOrganizeRequestTimeoutMs;
            request.KeepAlive = false;

            byte[] payload = Encoding.UTF8.GetBytes(AutoOrganizeRequestBody);
            request.ContentLength = payload.Length;
            using (Stream stream = request.GetRequestStream())
                stream.Write(payload, 0, payload.Length);

            try
            {
                using (HttpWebResponse response = (HttpWebResponse)request.GetResponse())
                using (StreamReader reader = new StreamReader(response.GetResponseStream(), Encoding.UTF8))
                    return FormatAutoOrganizeSuccess(reader.ReadToEnd());
            }
            catch (WebException exception)
            {
                throw new InvalidOperationException(ReadAutoOrganizeError(exception));
            }
        }

        private static string FormatAutoOrganizeSuccess(string json)
        {
            using (MemoryStream stream = new MemoryStream(Encoding.UTF8.GetBytes(json)))
            {
                DataContractJsonSerializer serializer =
                    new DataContractJsonSerializer(typeof(AutoOrganizeResponse));
                AutoOrganizeResponse response = serializer.ReadObject(stream) as AutoOrganizeResponse;
                if (response == null || !response.ok)
                    return "Packsmith 自动整理已完成，但返回格式无效";
                return string.Format(CultureInfo.InvariantCulture,
                    "Packsmith 自动整理完成: {0} | {1} | {2} ms | 交换 {3} 次 | 旋转 {4} 次",
                    response.solutionStatus ?? "?",
                    response.message ?? "",
                    response.solveMs ?? 0,
                    response.moves ?? 0,
                    response.rotations ?? 0);
            }
        }

        private static string ReadAutoOrganizeError(WebException exception)
        {
            HttpWebResponse response = exception.Response as HttpWebResponse;
            if (response != null)
            {
                try
                {
                    using (StreamReader reader = new StreamReader(response.GetResponseStream(), Encoding.UTF8))
                    {
                        string body = reader.ReadToEnd();
                        using (MemoryStream stream = new MemoryStream(Encoding.UTF8.GetBytes(body)))
                        {
                            DataContractJsonSerializer serializer =
                                new DataContractJsonSerializer(typeof(ApiErrorEnvelope));
                            ApiErrorEnvelope envelope = serializer.ReadObject(stream) as ApiErrorEnvelope;
                            if (envelope != null && envelope.error != null
                                    && !string.IsNullOrEmpty(envelope.error.message))
                                return envelope.error.message;
                        }
                    }
                }
                catch
                {
                }
            }
            return exception.Message;
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
                if (charm == null && tablet == null)
                    continue;
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
            foreach (KeyValuePair<int, int> multiplier in CaptureFixedLevelMultipliers(
                inventory, GetInt(inventory, "Width", 6), GetInt(inventory, "CurrentInventoryStorage", 0)))
            {
                canonical.Append("|M:").Append(multiplier.Key).Append(':').Append(multiplier.Value);
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
            SortedDictionary<int, int> fixedMultipliers = CaptureFixedLevelMultipliers(
                inventory, width, cellCount);

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
                        tablets.Append(",\"customCandidates\":").Append(
                            CompileCustomCandidates(tablet, query, conditionQuery, rotatable,
                                                    width, height, cellCount));
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
            json.Append(",\"doubleLevelCells\":[");
            bool firstDoubleCell = true;
            foreach (KeyValuePair<int, int> multiplier in fixedMultipliers)
            {
                if (multiplier.Value != 2)
                    continue;
                if (!firstDoubleCell) json.Append(',');
                firstDoubleCell = false;
                json.Append(multiplier.Key.ToString(CultureInfo.InvariantCulture));
            }
            json.Append(']');
            json.Append(",\"artifacts\":[").Append(artifacts).Append(']');
            json.Append(",\"tablets\":[").Append(tablets).Append(']');
            json.Append('}');
            return json.ToString();
        }

        private static SortedDictionary<int, int> CaptureFixedLevelMultipliers(
            object inventory, int width, int cellCount)
        {
            SortedDictionary<int, int> result = new SortedDictionary<int, int>();
            IEnumerable engravings = GetMember(inventory, "fixedEngravingsOnServer") as IEnumerable;
            if (engravings == null || width <= 0 || cellCount <= 0)
                return result;
            foreach (object engraving in engravings)
            {
                IEnumerable entries = GetMember(engraving, "fixedMultiplyLevel") as IEnumerable;
                if (entries == null)
                    continue;
                foreach (object entry in entries)
                {
                    object position = GetMember(entry, "Key");
                    int x = GetInt(position, "x", -1);
                    int y = GetInt(position, "y", -1);
                    int cell = y * width + x;
                    int value = GetInt(entry, "Value", 0);
                    if (x < 0 || y < 0 || cell < 0 || cell >= cellCount || value <= 0)
                        continue;
                    int existing;
                    result.TryGetValue(cell, out existing);
                    result[cell] = existing + value;
                }
            }
            return result;
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
            if (_customCandidateCache.Count > 256)
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

        [DataContract]
        private sealed class RuntimeInfo
        {
            [DataMember] public int port;
            [DataMember] public string token;
        }

        [DataContract]
        private sealed class AutoOrganizeResponse
        {
            [DataMember] public bool ok;
            [DataMember] public string message;
            [DataMember] public string solutionStatus;
            [DataMember] public int? solveMs;
            [DataMember] public int? moves;
            [DataMember] public int? rotations;
        }

        [DataContract]
        private sealed class ApiErrorEnvelope
        {
            [DataMember] public ApiError error;
        }

        [DataContract]
        private sealed class ApiError
        {
            [DataMember] public string code;
            [DataMember] public string message;
        }
    }
}
