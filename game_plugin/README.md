# Sephiria Inventory Bridge

This optional Windows-only BepInEx plugin reads the local player's `GridInventory`
and exposes one JSON snapshot through the local named pipe
`SephiriaInventoryBridge.v1`. Version 1.2 also exposes the local command pipe
`SephiriaInventoryBridge.apply.v1`, which can apply an explicitly confirmed solver
result in single-player/host games.

Before applying, the plugin verifies the game build, capacity, and exact item
instance IDs/types. Current positions and tablet rotations may differ from the
snapshot; the plugin always starts from the live arrangement. It runs on Unity's
main thread and calls the game's public `Swap` and
`DoClickAction` APIs; it never writes inventory matrices or item coordinates
directly. The result is verified after every operation and restored on failure
when possible.

Install from PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File .\game_plugin\install.ps1
```

Restart Sephiria after installation, enter a run, then use **读取游戏** in the
solver. Protocol v2 also exports effective custom-tablet queries, conditions,
rotatability, and compiled candidates for the current backpack size. Uninstall
only this plugin with:

```powershell
powershell -ExecutionPolicy Bypass -File .\game_plugin\uninstall.ps1
```

## In-game auto-organize (v1.4+)

When the Packsmith solver is running, the plugin can also expose a small
**整理** button beside the dungeon backpack grid. No hotkey is required.

1. Start the solver (`启动求解器.bat` or `python -m app.server`). The server
   writes `%LOCALAPPDATA%\SephiriaPacksmith\runtime.json` with its local port
   and access token.
2. Enter a single-player or host run and open the character backpack
   (`UI_CharacterStatusPanel`).
3. Click **整理**. The plugin calls `POST /api/auto-organize` on the local
   solver with `fastMode: true`, a 30 s time limit, and automatic worker count.
4. Packsmith reads the live inventory from the existing named pipe, searches for
   a strong layout, and applies it through the same verified `Swap` /
   `DoClickAction` path as the Web **应用到游戏** button.

While organizing, the button shows **…** and ignores duplicate clicks. Closing
the backpack hides the button.

Defaults for this one-click flow (not configurable in-game):

- **Fast mode** on — stops once the search stops improving; usually returns in
  a few seconds on typical backpacks.
- **Artifact weight** 5, no fixed cells, no special-priority toggles — the same
  neutral defaults as an empty Web build, not your saved browser constraints.
- **Scope** — main inventory grid only; potion slots and sub-bags are unchanged.

Requirements and limits:

- The solver must stay running in the background; the plugin reads
  `runtime.json` for connection details.
- Apply still requires single-player or host authority, a complete inventory
  snapshot, and mappable artifacts/tablets. Unmapped items abort with an error.
- Multiplayer guest clients are not supported for automatic apply.

Rebuild after changing the plugin:

```powershell
powershell -ExecutionPolicy Bypass -File .\game_plugin\build.ps1
```

Copy `artifacts\game_plugin\SephiriaInventoryBridge.dll` into the game's
`BepInEx\plugins\` folder and restart Sephiria.

The installer pins BepInEx `5.4.23.5` and verifies the official release archive
SHA-256 before copying files. It intentionally leaves BepInEx installed during
plugin removal because other game plugins may depend on it.
