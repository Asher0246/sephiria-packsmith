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

The installer pins BepInEx `5.4.23.5` and verifies the official release archive
SHA-256 before copying files. It intentionally leaves BepInEx installed during
plugin removal because other game plugins may depend on it.
