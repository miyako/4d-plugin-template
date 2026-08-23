---
applyTo: "**"
---

# 4D Plugin Development Guide

A comprehensive reference for building a 4D plugin from scratch in C/C++. This document captures the full workflow, conventions, caveats, and tips needed to create, build, test, and ship a 4D plugin on macOS and Windows.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Project Setup](#project-setup)
3. [SDK Integration](#sdk-integration)
4. [Plugin Entry Points](#plugin-entry-points)
5. [manifest.json — Command Definitions](#manifestjson--command-definitions)
6. [constants.xlf — Constant Definitions](#constantsxlf--constant-definitions)
7. [C/C++ Implementation](#cc-implementation)
8. [Xcode Project (macOS)](#xcode-project-macos)
9. [Visual Studio Project (Windows)](#visual-studio-project-windows)
10. [4D Test Project](#4d-test-project)
11. [Automated Testing with tool4d](#automated-testing-with-tool4d)
12. [GitHub Actions CI/CD](#github-actions-cicd)
13. [GitHub Actions — Release Workflow](#github-actions--release-workflow)
14. [README Template](#readme-template)
15. [Common Pitfalls](#common-pitfalls)
16. [Step-by-Step Checklist](#step-by-step-checklist)

---

## Architecture Overview

A 4D plugin is a native dynamic library loaded by the 4D runtime at startup:

| Platform | File Type | Extension | Loader |
|---|---|---|---|
| macOS | Bundle (`.bundle`) | None | `dlopen` |
| Windows | DLL | `.4DX` | `LoadLibrary` |

### Execution Flow

```
4D starts
  → scans Plugins folder
  → loads library (dlopen / LoadLibrary)
  → finds exported symbol `FourDPackex`
  → calls FourDPackex(callback_address)
  → plugin stores callback in global `gCall4D`
  → 4D calls `PluginMain(selector, params)` for each:
      - Plugin command call (selector = command index, 1-based)
      - Plugin area event (eAE_Idle to eAE_DesignInit)
      - Plugin event (kNotifyDemoPlugins to kInitPlugin)
```

### Key Rules

- The plugin may **only** call SDK entry points during a `PluginMain` cycle.
- It may **not** call the runtime from a different thread or unrelated execution cycle.
- Exception: some functions like `PA_FreezeProcess` / `PA_UnfreezeProcess` that do require a language context.
- For system calls that require the main thread, use `PA_RunInMainProcess`.
- 4D data types are **opaque structures** — always convert to/from C types via SDK utilities.

---

## Project Setup

### Repository Structure

```
project-root/
├── .github/
│   └── workflows/
│       ├── test.yml              # CI test workflow (tag + manual)
│       ├── bump-version.yml      # Version bump + tag push
│       └── release.yml           # Build, sign, notarize & release
├── .gitignore
├── 4D-Plugin-SDK/                # Git submodule
├── VERSION                       # Semantic version file (e.g. "1.0.0")
├── README.md
└── {plugin-name}/                # e.g., "example/"
    ├── CMakeLists.txt            # Cross-platform build (CMake)
    ├── Info.plist.in             # macOS bundle Info.plist template (CRITICAL)
    ├── {name}-4dplugin.cpp       # Plugin implementation
    ├── {name}-4dplugin.h         # Plugin header
    ├── manifest.json             # Command definitions
    ├── constants.xlf             # Constant definitions
    └── {name}-test/              # 4D test project
        ├── Plugins/              # Built plugin output (gitignored)
        ├── Project/
        │   ├── {name}.4DProject
        │   └── Sources/
        │       ├── Methods/
        │       │   ├── test_all.4dm
        │       │   └── test_{command}.4dm
        │       └── folders.json
        ├── Resources/
        └── Settings/
```

### VERSION File

Create a `VERSION` file in the project root containing the semantic version:

```
1.0.0
```

This file is the single source of truth for the version number, used by `bump-version.yml` to tag releases.

### .gitignore

```
{name}/{name}-test/Data/*
{name}/{name}-test/Project/DerivedData/*
{name}/{name}-test/userPreferences.*
{name}/{name}-test/Plugins/
{name}/cmake-build/
{name}/build/
**/xcuserdata/
*.vcxproj.user
.DS_Store
```

---

## SDK Integration

### Add as Git Submodule

```bash
git submodule add https://github.com/4d/4D-Plugin-SDK.git 4D-Plugin-SDK
```

Using a submodule (not a plain clone) because:
- Pins to a specific SDK commit for reproducible builds
- Keeps the repo lightweight (no duplicated history)
- Easy updates via `git submodule update --remote`
- Anyone cloning gets the SDK with `--recurse-submodules`

### Key SDK Files

Located in `4D-Plugin-SDK/4D Plugin API/`:

| File | Purpose |
|---|---|
| `4DPluginAPI.h` | SDK function declarations |
| `4DPluginAPI.c` | SDK function implementations (must be compiled into plugin) |
| `4DPluginAPI.def` | Windows module definition file (exports `FourDPackex`) |
| `EntryPoints.h` | Runtime callback function declarations |
| `PublicTypes.h` | Public types, area events, plugin events |
| `PrivateTypes.h` | Internal types |
| `Flags.h` | Feature flags |

### Critical: `4DPluginAPI.c` Must Be Compiled

The SDK source file `4DPluginAPI.c` **must** be compiled and linked into the plugin. It contains:
- The `FourDPackex` entry point (the symbol 4D searches for at load time)
- The `gCall4D` global (callback table populated by 4D)
- All SDK wrapper functions

Without it, the plugin will not be recognized by 4D.

---

## Plugin Entry Points

### `FourDPackex` (exported symbol)

- The **sole exported symbol** from the plugin DLL/bundle.
- Called **once** when 4D loads the plugin.
- Receives the callback address to the 4D runtime.
- Defined in `4DPluginAPI.c` and exported via `4DPluginAPI.def` (Windows) or dynamic symbol visibility (macOS).

### `PluginMain(selector, params)`

- The dispatcher function called by 4D for each interaction.
- `selector` is the 1-based command index from `manifest.json`.
- The developer implements a `switch` on `selector` to route to command functions.

```c
void PluginMain(PA_long32 selector, PA_PluginParameters params) {
    switch(selector) {
        case 1:
            my_first_command(params);
            break;
        case 2:
            my_second_command(params);
            break;
    }
}
```

### Historical Note

In earlier versions, `FourdPack` (without `Ex`) was the entry point for ANSI string mode. Modern 4D is fully unicode — always use `FourDPackex`.

---

## manifest.json — Command Definitions

```json
{
    "name": "{plugin-name}",
    "id": 20000,
    "commands": [
        {
            "theme": "{group-name}",
            "syntax": "{command_name}(&T;&L):T",
            "threadSafe": true
        }
    ]
}
```

### Fields

| Field | Description |
|---|---|
| `name` | Plugin identity. Must be unique across all installed plugins. If duplicates exist, none load. |
| `id` | 15001–32767 (below 15000 is reserved by 4D). Legacy field, not functionally important. |
| `theme` | Groups commands in the 4D IDE (design mode). |
| `threadSafe` | Developer's guarantee that the code is thread-safe. Enables use in preemptive 4D processes. A `threadSafe:false` command used in a preemptive process causes a runtime/compiler error. |

### Syntax Tokens

The syntax string describes expected parameter types by position and optionally a return value.

Format: `command_name({params}):{return_type}`
- Arguments separated by semicolons: `&T;&L;&R`
- Return type after closing parenthesis: `):T`
- Maximum 25 arguments

| Token | 4D Type | SDK Getter | SDK Setter |
|---|---|---|---|
| `&T` | Text | `PA_GetStringParameter` | `PA_ReturnString` |
| `&L` | Longint | `PA_GetLongParameter` | `PA_ReturnLong` |
| `&R` | Real | `PA_GetDoubleParameter` | `PA_ReturnDouble` |
| `&D` | Date | `PA_GetDateParameter` | `PA_ReturnDate` |
| `&H` | Time | `PA_GetTimeParameter` | `PA_ReturnTime` |
| `&I` | Integer | `PA_GetShortParameter` | `PA_ReturnShort` |
| `&O` | BLOB | `PA_GetBlobParameter` | `PA_ReturnBlob` |
| `&P` | Picture | `PA_GetPictureParameter` | `PA_ReturnPicture` |
| `&Y` | Array | `PA_GetVariableParameter` | — |
| `&J` | Object | `PA_GetObjectParameter` | `PA_ReturnObject` |
| `&C` | Collection | `PA_GetCollectionParameter` | `PA_ReturnCollection` |
| `&Z` | Pointer | `PA_GetPointerParameter` | — |
| `&8` | Double (deprecated) | — | — |
| `&S` | String (deprecated) | — | — |
| `&U` | UTXT (deprecated) | — | — |

### Command Order Matters

The order of commands in the `commands` array determines the `selector` number passed to `PluginMain`:
- First command → selector 1
- Second command → selector 2
- etc.

### Naming Rules (shared with constants)

- Up to 31 ANSI characters
- Case insensitive
- Must not start with a number
- May contain spaces between words (but discouraged)
- May not contain operators
- Must not clash with 4D reserved identifiers (command names, built-in constants)

### Command Naming Convention

4D follows a naming convention based on whether a command returns a value:

- **Commands that return a value**: Use title case (capitalize first word only after prefix)
  - Example: `Archive Create`, `Archive List`, `HTML2MD`
- **Commands that do NOT return a value**: Use ALL CAPS (except the prefix)
  - Example: `Archive SET PASSPHRASE`, `SMTP SET OPTION`

The prefix (theme word) always keeps its original casing.

### Omitted Parameters

When a 4D caller omits a trailing parameter, the plugin receives the **default value for that type** (0 for Longint, empty string for Text, etc.). This allows optional parameters without special syntax.

---

## constants.xlf — Constant Definitions

```xml
<?xml version="1.0" encoding="UTF-8" standalone="no" ?>
<xliff version="1.0" xmlns:d4="http://www.4d.com/d4-ns">
<header>
    <note>{plugin-name}</note>
</header>
<group resname="themes">
    <trans-unit id="{theme-id}" resname="{theme-id}" translate="no">
        <source>{Theme Display Name}</source>
    </trans-unit>
</group>
<group restype="x-4DK#" d4:groupName="{theme-id}">
    <trans-unit d4:value="{value}" id="{constant-id}">
        <source>{constant_name}</source>
    </trans-unit>
</group>
</xliff>
```

### Value Types

- Numeric values are inferred if no suffix
- Explicit type suffixes: `:L` (Longint), `:R` (Real), `:S` (Text)
- Example: `"42:S"` is the text string "42", not the number 42

### Order Matters

The order of constants determines their internal token code (similar to commands).

---

## C/C++ Implementation

### Header File Pattern

```c
#ifndef {NAME}_4DPLUGIN_H
#define {NAME}_4DPLUGIN_H

#include "4DPluginAPI.h"
#include <time.h>
#include <string.h>

static void {command_name}(PA_PluginParameters params);

#endif
```

### Implementation Pattern

```c
#include "{name}-4dplugin.h"

void PluginMain(PA_long32 selector, PA_PluginParameters params) {
    switch(selector) {
        case 1:
            {command_name}(params);
            break;
    }
}

static void {command_name}(PA_PluginParameters params) {
    // Get parameters (1-indexed)
    PA_Unistring *textParam = PA_GetStringParameter(params, 1);
    PA_long32 longParam = PA_GetLongParameter(params, 2);

    // Access string data
    PA_Unichar *chars = PA_GetUnistring(textParam);
    PA_long32 len = PA_GetUnistringLength(textParam);
    // Note: textParam is owned by the runtime — do NOT dispose it

    // Build result (PA_Unichar = unsigned short = UTF-16)
    PA_Unichar result[1024];
    // ... populate result ...
    result[resultLen] = 0; // null-terminate

    // Return value
    PA_ReturnString(params, result);
}
```

### String Handling

- `PA_Unichar` is `unsigned short` (16-bit, UTF-16).
- On macOS, `wchar_t` is 32-bit, so you **cannot** use `L"string"` literals for `PA_Unichar`.
- For ASCII prefixes, widen manually: `result[i] = (PA_Unichar)ascii_char;`
- `PA_GetStringParameter` returns a runtime-owned pointer — do **not** call `PA_DisposeUnistring` on it.
- For dynamically created strings, use `PA_CreateUnistring` and `PA_DisposeUnistring`.

### Platform-Specific Code

Use `#ifdef _WIN32` for Windows-specific code:

```c
// Example: localtime_s (Windows) vs localtime (POSIX)
#ifdef _WIN32
    localtime_s(&local, &now);
#else
    local = *localtime(&now);
#endif
```

MSVC treats many standard C functions as deprecated (`localtime`, `strcpy`, etc.). Use the `_s` variants on Windows to avoid build errors.

### Working with 4D Pictures

A 4D `Picture` is a **container** of multiple representations (formats). Each representation has a UTI string (semicolon-delimited identifiers including file extensions and MIME types). To extract pixel data (e.g. PNG), you must iterate representations to find the right format.

**Pattern: Find a picture representation by type**

```c
// Returns the 1-based index of a representation matching the given type identifier.
// type can be a MIME type ("image/png"), file extension (".png"), or UTI ("public.png").
static PA_long32 get_picture_index(PA_Picture p, const char *type) {
    PA_long32 i = 0;
    if (!p) return 0;

    PA_ErrorCode err = eER_NoErr;
    while (err == eER_NoErr) {
        PA_Unistring utype = PA_GetPictureData(p, ++i, NULL);
        err = PA_GetLastError();
        if (err != eER_NoErr) break;

        // Convert UTF-16 UTI string to UTF-8
        uint32_t len = (uint32_t)(utype.fLength * 4) + 1;
        std::vector<uint8_t> buf(len);
        PA_ConvertCharsetToCharset(
            (char *)utype.fString, utype.fLength * sizeof(PA_Unichar), eVTC_UTF_16,
            (char *)&buf[0], len, eVTC_UTF_8);
        std::string uti((const char *)&buf[0]);

        // UTI is semicolon-delimited, e.g. ".png;image/png;public.png;PNG"
        size_t pos = 0, found = 0;
        for (pos = uti.find(';'); pos != std::string::npos; pos = uti.find(';', found)) {
            if (uti.substr(found, pos - found) == type) return i;
            found = pos + 1;
        }
        if (uti.substr(found) == type) return i;
    }
    return 0;
}
```

**Pattern: Extract raw image bytes from a Picture**

```c
PA_long32 idx = get_picture_index(picture, "image/png");
if (idx) {
    PA_Handle h = PA_NewHandle(0);
    PA_GetPictureData(picture, idx, h);
    if (PA_GetLastError() == eER_NoErr) {
        uint8_t *data = (uint8_t *)PA_LockHandle(h);
        size_t size = PA_GetHandleSize(h);
        // ... use PNG data ...
        PA_UnlockHandle(h);
    }
    PA_DisposeHandle(h);
}
```

**Pattern: Return a Picture to 4D from raw bytes**

```c
// Create a picture from raw PNG/JPEG bytes
PA_Picture pic = PA_CreatePicture((void *)png_data, png_size);
PA_ReturnPicture(params, pic);
// Do NOT call PA_DisposePicture here — PA_ReturnPicture takes ownership.
// Disposing after return causes a use-after-free crash in the runtime.
```

**Notes:**
- **`PA_ReturnPicture` takes ownership** of the `PA_Picture` — do NOT call `PA_DisposePicture` after it. The runtime disposes it when done.
- Always try multiple format identifiers as fallback (e.g. `"image/png"` then `".png"`)
- The representation index is 1-based
- `PA_GetPictureData` with `NULL` handle returns only the UTI string (for enumeration)
- `PA_GetPictureData` with a valid handle fills it with the raw bytes
- For output, `PA_CreatePicture` auto-detects format from the data header

---

## Xcode Project (macOS)

### Project Type

- **Product type**: `com.apple.product-type.bundle` (creates a `.bundle`)
- **Wrapper extension**: `bundle`

### Source Files

1. `{name}-4dplugin.cpp` — added to **Sources** build phase
2. `4DPluginAPI.c` from SDK — via **PBXFileSystemSynchronizedRootGroup**
   - Must set `explicitFileTypes` to compile `.c` as C++: `4DPluginAPI.c = sourcecode.cpp.cpp`
   - **Critical**: must add `fileSystemSynchronizedGroups` to the native target, otherwise the SDK source won't compile

### Frameworks

- **CoreGraphics.framework** must be linked (SDK uses `CGContextScaleCTM` and `CGContextTranslateCTM` in QuickDraw/Quartz axis functions)

### Resource Files

- `manifest.json` and `constants.xlf` go in the **Copy Bundle Resources** build phase
- They end up in `{name}.bundle/Contents/Resources/`

### Build Configuration (xcconfig)

Debug xcconfig:
```
CONFIGURATION_BUILD_DIR = $(PROJECT_DIR)/$(PROJECT_NAME)-test/Plugins
```

Release xcconfig (same for testing purposes):
```
CONFIGURATION_BUILD_DIR = $(PROJECT_DIR)/$(PROJECT_NAME)-test/Plugins
```

Set `baseConfigurationReference` on the respective `XCBuildConfiguration` to point to each xcconfig file.

### Code Signing for CI

In CI (GitHub Actions), disable code signing:
```
xcodebuild ... CODE_SIGN_IDENTITY=- CODE_SIGNING_REQUIRED=NO CODE_SIGNING_ALLOWED=NO
```

### Verifying the Build

Check exported symbols with `nm`:
```bash
nm -g path/to/example.bundle/Contents/MacOS/example
```

Expected symbols:
- `_FourDPackex` — entry point
- `_PluginMain` — command dispatcher
- `_gCall4D` — callback table

If `_gCall4D` and `_FourDPackex` are missing, `4DPluginAPI.c` is not being compiled/linked.

---

## Visual Studio Project (Windows)

### Project Type

- **ConfigurationType**: `DynamicLibrary` (only for x64; Win32 and ARM64 are `Application` placeholders)
- **Target extension**: `.4DX`

### Compiler Settings (x64)

- **Character set**: Unicode
- **Runtime library**: `/MTd` (Debug), `/MT` (Release) — static CRT
- **Include path**: `$(SolutionDir)..\4D-Plugin-SDK\4D Plugin API`
- **Conformance mode**: true

### Linker Settings (x64)

- **Module definition file**: `$(SolutionDir)..\4D-Plugin-SDK\4D Plugin API\4DPluginAPI.def`
  - This exports `FourDPackex` without needing `__declspec(dllexport)` in source code

### Output Path

```xml
<OutDir>$(SolutionDir)$(ProjectName)-test\Plugins\$(ProjectName)\Contents\Windows64</OutDir>
```

Set for both Debug|x64 and Release|x64.

### Post-Build Event (resource copy)

```xml
<PostBuildEvent>
  <Command>xcopy /Y "$(ProjectDir)manifest.json" "$(OutDir)..\Resources\"
xcopy /Y "$(ProjectDir)constants.xlf" "$(OutDir)..\Resources\"</Command>
</PostBuildEvent>
```

### Plugin Folder Structure (Windows)

```
Plugins/
  {plugin-name}/
    Contents/
      Windows64/
        {plugin-name}.4DX
      Resources/
        manifest.json
        constants.xlf
```

---

## 4D Test Project

### Structure

```
{name}-test/
├── Project/
│   ├── {name}.4DProject          # Project anchor file
│   └── Sources/
│       ├── Methods/
│       │   ├── test_all.4dm      # Test runner for CI
│       │   └── test_{cmd}.4dm    # Individual test methods
│       └── folders.json          # Virtual explorer folders
├── Plugins/                      # Built plugin output
├── Resources/
└── Settings/
```

### .4DProject File

```json
{
    "$comment": "The project file serves as an anchor to locate other project files",
    "compatibilityVersion": 2101
}
```

Version encoding:
- `2101` → 4D v21.1
- `2120` → 4D v21 R2
- `21A0` → 4D v21 R10

### Test Methods

#### Individual test (`test_{command}.4dm`)

```4d
//%attributes = {"invisible":true,"preemptive":"capable"}

// Test explicit behavior
ASSERT(my_command("input"; my_constant_a)="expected output")

// Test edge cases
ASSERT(my_command(""; my_constant_a)="expected for empty")
ASSERT(my_command("日本語"; my_constant_a)="expected for unicode")

/*
    Test dynamic/conditional behavior.
    Use block comments for multi-line explanations.
*/
var $var : Type
// ... conditional test logic ...
```

Key points:
- `//%attributes` is a JSON comment for method metadata
- `"preemptive":"capable"` must match `threadSafe: true` in manifest
- `"invisible":true` hides the method from end users
- `ASSERT` prompts a dialog on failure; in headless mode, this aborts the process
- The IDE auto-adds command token suffixes (e.g., `ASSERT:C1129`) — do not add them manually when editing `.4dm` files
- Use `//` for line comments, `/* */` for block comments
- 4D time literals: `?03:00:00?`
- 4D conditional: `Case of / : (condition) / Else / End case`

#### Test runner (`test_all.4dm`)

```4d
//%attributes = {"invisible":true}
If (Application info.headless)

    test_{command1}
    test_{command2}

    LOG EVENT(Into system standard outputs; "PASS"; Information message)

End if
```

**Note on command token suffixes**: You may see suffixes like `:C1129` after command names (e.g., `ASSERT:C1129`) in `.4dm` files. These are added automatically by the 4D IDE to disambiguate between English and French command names. **You do not need to add them yourself** — tool4d is fixed to English coding and resolves plain command names without suffixes. When generating `.4dm` files programmatically, just use the plain English names (e.g., `ASSERT`, `LOG EVENT`, `Current time`).

- Guards with `Application info.headless` — only runs in CLI mode (tool4d)
- Calls all individual test methods
- Outputs "PASS" to stdout on success
- If any `ASSERT` fails in headless mode: dialog → auto-abort → process exits with non-zero code → "PASS" is never printed

#### Virtual Explorer Folders (`folders.json`)

```json
{
    "Tests": {
        "methods": [
            "test_all",
            "test_{command}"
        ]
    },
    "trash": {}
}
```

---

## 4D Test Project Boilerplate

When creating a 4D test project from scratch (without the 4D IDE), the following files are required. Replace `{name}` with the plugin name throughout.

### Directory Structure

```
{name}-test/
├── Project/
│   ├── {name}.4DProject
│   └── Sources/
│       ├── Methods/
│       │   ├── test_all.4dm
│       │   └── test_{command}.4dm
│       ├── catalog.4DCatalog
│       ├── catalog_editor.json
│       ├── folders.json
│       ├── menus.json
│       ├── roles.json
│       └── settings.4DSettings
├── Resources/
└── Settings/
    └── backup.4DSettings
```

### File Templates

#### `{name}.4DProject`

```json
{
    "$comment": "The project file serves as an anchor to locate other project files",
    "compatibilityVersion": 2101
}
```

Set `compatibilityVersion` to match your target 4D version. See [version encoding](#automated-testing-with-tool4d).

#### `catalog.4DCatalog`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE base SYSTEM "http://www.4d.com/dtd/2007/base.dtd" >
<base name="{name}" uuid="{GENERATE-A-UUID}" collation_locale="en-gb">
    <schema name="DEFAULT_SCHEMA"/>
    <base_extra>
        <journal_file journal_file_enabled="false"/>
    </base_extra>
</base>
```

Generate a unique UUID (32 hex characters, uppercase) for each project.

#### `catalog_editor.json`

```json
{
    "tables": {}
}
```

#### `folders.json`

```json
{
    "Tests": {
        "methods": [
            "test_all",
            "test_{command}"
        ]
    },
    "trash": {}
}
```

#### `menus.json`

```json
{
    "bars": [
        {
            "id": 1,
            "name": "Menu Bar",
            "items": [
                {"link": 32001},
                {"link": 32002}
            ]
        }
    ],
    "menus": [
        {
            "link": 32001,
            "title": ":xliff:CommonMenuFile",
            "items": [
                {
                    "title": ":xliff:CommonMenuItemQuit",
                    "shortcutAccel": true,
                    "shortcutKey": "Q",
                    "action": "quit"
                }
            ]
        },
        {
            "link": 32002,
            "title": ":xliff:CommonMenuEdit",
            "items": [
                {
                    "title": ":xliff:CommonMenuItemUndo",
                    "shortcutAccel": true,
                    "shortcutKey": "Z",
                    "action": "undo"
                },
                {"title": "(-", "isSeparator": true},
                {
                    "title": ":xliff:CommonMenuItemCut",
                    "shortcutAccel": true,
                    "shortcutKey": "X",
                    "action": "cut"
                },
                {
                    "title": ":xliff:CommonMenuItemCopy",
                    "shortcutAccel": true,
                    "shortcutKey": "C",
                    "action": "copy"
                },
                {
                    "title": ":xliff:CommonMenuItemPaste",
                    "shortcutAccel": true,
                    "shortcutKey": "V",
                    "action": "paste"
                },
                {
                    "title": ":xliff:CommonMenuItemSelectAll",
                    "shortcutAccel": true,
                    "shortcutKey": "A",
                    "action": "selectAll"
                }
            ]
        }
    ]
}
```

#### `roles.json`

```json
{
    "forceLogin": false,
    "restrictedByDefault": false,
    "privileges": [],
    "roles": [],
    "permissions": {
        "allowed": [
            {
                "applyTo": "ds",
                "type": "datastore",
                "read": [],
                "create": [],
                "update": [],
                "drop": [],
                "execute": [],
                "promote": []
            }
        ]
    }
}
```

#### `settings.4DSettings`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<preferences stamp="2">
    <com.4d>
        <compiler>
            <options target="all"/>
        </compiler>
    </com.4d>
</preferences>
```

#### `backup.4DSettings`

```xml
<?xml version="1.0" encoding="UTF-8" standalone="no" ?>
<Preferences4D xmlns="http://www.4d.com/namespace/reserved/2004/backup">
  <Backup>
    <Settings>
      <Scheduler>
        <Frequency>Never</Frequency>
      </Scheduler>
    </Settings>
  </Backup>
</Preferences4D>
```

---

## Automated Testing with tool4d

### What is tool4d?

- A CLI version of 4D designed for CI/CD
- **No license activation required**
- Must match the `compatibilityVersion` of the test project

### Running Tests

```bash
/path/to/tool4d --dataless --startup-method=test_all --project=/path/to/{name}.4DProject
```

| Flag | Description |
|---|---|
| `--dataless` | No data file (empty data path). Suitable for tests that don't need records. |
| `--startup-method` | 4D method to execute at startup |
| `--project` | Path to the `.4DProject` file |

### Exit Behavior

- **PASS**: stdout contains "PASS", exit code 0
- **FAIL**: ASSERT triggers a dialog → headless auto-abort → non-zero exit code, no "PASS" output

### Download URLs

```
https://resources-download.4d.com/release/{branch}/{version}/latest/{platform}/tool4d_{suffix}.tar.xz
```

| Parameter | Examples |
|---|---|
| branch | `21.x`, `20.x` |
| version | `21.1`, `21 R2` |
| platform | `win`, `mac` |
| suffix | `win`, `x86_64`, `arm64` |

No authentication required for download.

---

## GitHub Actions CI/CD

### `test.yml` — Build & Test (CMake)

This workflow builds the plugin using CMake on both platforms and runs tests with tool4d. Triggered on tag push or manual dispatch.

```yaml
name: Build and Test

on:
  push:
    tags:
      - '*'
  workflow_dispatch:

jobs:
  test:
    strategy:
      fail-fast: false
      matrix:
        include:
          - os: macos-latest
            platform: mac
            tool4d_archive: tool4d_arm64.tar.xz
          - os: windows-latest
            platform: win
            tool4d_archive: tool4d_win.tar.xz

    runs-on: ${{ matrix.os }}

    steps:
      - name: Checkout
        uses: actions/checkout@v4
        with:
          submodules: recursive
          fetch-depth: 0

      - name: Parse 4D version from .4DProject
        id: version
        shell: bash
        run: |
          compat=$(cat {name}/{name}-test/Project/{name}.4DProject | python3 -c "
          import sys, json
          data = json.load(sys.stdin)
          print(data['compatibilityVersion'])
          ")
          major=${compat:0:2}
          minor_raw=${compat:2:2}
          minor_dec=$((10#$minor_raw))
          if [ $minor_dec -lt 10 ]; then
            version="${major}.${minor_dec}"
          else
            r_version=$((minor_dec / 10))
            version="${major} R${r_version}"
          fi
          branch="${major}.x"
          echo "branch=${branch}" >> $GITHUB_OUTPUT
          echo "version=${version}" >> $GITHUB_OUTPUT

      - name: Download tool4d
        shell: bash
        run: |
          url="https://resources-download.4d.com/release/${{ steps.version.outputs.branch }}/${{ steps.version.outputs.version }}/latest/${{ matrix.platform }}/${{ matrix.tool4d_archive }}"
          curl "${url}" -o tool4d.tar.xz -sL
          tar xJf tool4d.tar.xz

      - name: Build plugin (macOS)
        if: runner.os == 'macOS'
        shell: bash
        run: |
          cd {name}
          mkdir -p cmake-build && cd cmake-build
          cmake .. -DCMAKE_BUILD_TYPE=Release
          cmake --build .

      - name: Build plugin (Windows)
        if: runner.os == 'Windows'
        shell: pwsh
        run: |
          cd {name}
          mkdir cmake-build; cd cmake-build
          cmake .. -A x64
          cmake --build . --config Release

      - name: Run tests (macOS)
        if: runner.os == 'macOS'
        shell: bash
        run: |
          tool4d.app/Contents/MacOS/tool4d \
            --dataless \
            --startup-method=test_all \
            --project=$(pwd)/{name}/{name}-test/Project/{name}.4DProject

      - name: Run tests (Windows)
        if: runner.os == 'Windows'
        shell: pwsh
        run: >
          ./tool4d/tool4d.exe --dataless --startup-method=test_all
          --project="$((Get-Location).Path)\{name}\{name}-test\Project\{name}.4DProject"
```

---

## GitHub Actions — Release Workflow

### `bump-version.yml` — Version Bump & Tag

Reads/writes a `VERSION` file in the project root. On manual dispatch, bumps the version, commits, and pushes a `vX.Y.Z` tag that triggers `release.yml`.

```yaml
name: Bump version

on:
  workflow_dispatch:
    inputs:
      mode:
        type: choice
        description: Semantic version bump
        options:
          - patch
          - minor
          - major
        default: patch
        required: true

permissions:
  contents: write

jobs:
  bump:
    runs-on: ubuntu-latest
    steps:
      - name: checkout
        uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: bump VERSION
        id: bump
        run: |
          set -euo pipefail
          CURRENT=$(cat VERSION)
          if [ -z "$CURRENT" ]; then
            echo "Could not read VERSION file" >&2
            exit 1
          fi

          IFS='.' read -r MAJOR MINOR PATCH <<< "$CURRENT"
          case "${{ inputs.mode }}" in
            major) MAJOR=$((MAJOR+1)); MINOR=0; PATCH=0 ;;
            minor) MINOR=$((MINOR+1)); PATCH=0 ;;
            patch) PATCH=$((PATCH+1)) ;;
          esac
          NEW="${MAJOR}.${MINOR}.${PATCH}"

          echo "Bumping ${CURRENT} -> ${NEW}"
          echo "$NEW" > VERSION

          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add VERSION
          git commit -m "chore: bump version to ${NEW}"
          git tag "v${NEW}"
          git push origin HEAD:"${GITHUB_REF_NAME}"
          git push origin "v${NEW}"

          echo "version=${NEW}" >> "$GITHUB_OUTPUT"

      - name: summary
        run: echo "### Bumped to v${{ steps.bump.outputs.version }} — release.yml will pick up the pushed tag." >> "$GITHUB_STEP_SUMMARY"
```

### `release.yml` — Build, Sign, Notarize & Release

Builds the plugin for both platforms using CMake, codesigns + notarizes the macOS binary, merges Windows + macOS into a single cross-platform `.bundle`, and publishes a GitHub Release.

```yaml
name: Build & Release

on:
  push:
    tags:
      - 'v*.*.*'
  workflow_dispatch:
    inputs:
      tag:
        description: Existing tag to build (e.g. v2.3.0)
        required: true

permissions:
  contents: write

env:
  PRODUCT_NAME: {name}
  MACOS_DEPLOYMENT_TARGET: '10.13'

concurrency:
  group: release-${{ github.ref }}
  cancel-in-progress: false

jobs:

  version:
    runs-on: ubuntu-latest
    outputs:
      tag: ${{ steps.set.outputs.tag }}
      version: ${{ steps.set.outputs.version }}
    steps:
      - id: set
        run: |
          TAG="${{ inputs.tag || github.ref_name }}"
          echo "tag=${TAG}" >> "$GITHUB_OUTPUT"
          echo "version=${TAG#v}" >> "$GITHUB_OUTPUT"

  build-windows:
    name: Build Windows (x64)
    needs: version
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ needs.version.outputs.tag }}
          submodules: recursive
          fetch-depth: 0

      - name: build with CMake
        shell: pwsh
        run: |
          cd ${{ env.PRODUCT_NAME }}
          mkdir cmake-build; cd cmake-build
          cmake .. -A x64
          cmake --build . --config Release

      - name: upload windows binaries
        uses: actions/upload-artifact@v4
        with:
          name: windows-binaries
          path: |
            ${{ env.PRODUCT_NAME }}/${{ env.PRODUCT_NAME }}-test/Plugins/${{ env.PRODUCT_NAME }}/Contents/Windows64/${{ env.PRODUCT_NAME }}.4DX
          if-no-files-found: error

  build-macos:
    name: Build macOS, package, sign, notarize & release
    needs: [version, build-windows]
    runs-on: macos-latest
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ needs.version.outputs.tag }}
          submodules: recursive
          fetch-depth: 0

      - name: download windows binaries
        uses: actions/download-artifact@v4
        with:
          name: windows-binaries
          path: winbuild

      - name: import Developer ID certificate
        env:
          CERT_BASE64: ${{ secrets.APPLE_DEVELOPER_ID_CERTIFICATE }}
          CERT_PASSWORD: ${{ secrets.APPLE_DEVELOPER_ID_CERTIFICATE_PASSWORD }}
          KEYCHAIN_PASSWORD: ${{ secrets.KEYCHAIN_PASSWORD }}
        run: |
          set -euo pipefail
          KEYCHAIN_PATH="$RUNNER_TEMP/build.keychain-db"
          CERT_PATH="$RUNNER_TEMP/certificate.p12"

          echo -n "$CERT_BASE64" | base64 --decode -o "$CERT_PATH"

          security create-keychain -p "$KEYCHAIN_PASSWORD" "$KEYCHAIN_PATH"
          security set-keychain-settings -lut 21600 "$KEYCHAIN_PATH"
          security unlock-keychain -p "$KEYCHAIN_PASSWORD" "$KEYCHAIN_PATH"
          security import "$CERT_PATH" -P "$CERT_PASSWORD" -A -t cert -f pkcs12 -k "$KEYCHAIN_PATH"
          security list-keychain -d user -s "$KEYCHAIN_PATH" login.keychain
          security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$KEYCHAIN_PASSWORD" "$KEYCHAIN_PATH"

          echo "KEYCHAIN_PATH=$KEYCHAIN_PATH" >> "$GITHUB_ENV"

      - name: store notarytool credentials
        env:
          APPLE_ID: ${{ secrets.NOTARYTOOL_APPLE_ID }}
          TEAM_ID: ${{ secrets.NOTARYTOOL_TEAM_ID }}
          APP_PASSWORD: ${{ secrets.NOTARYTOOL_PASSWORD }}
        run: |
          xcrun notarytool store-credentials "notarytool-profile" \
            --apple-id "$APPLE_ID" \
            --team-id "$TEAM_ID" \
            --password "$APP_PASSWORD" \
            --keychain "$KEYCHAIN_PATH"

      - name: build macOS plugin (universal)
        run: |
          cd ${{ env.PRODUCT_NAME }}
          mkdir -p cmake-build && cd cmake-build
          cmake .. -DCMAKE_BUILD_TYPE=Release \
            -DCMAKE_OSX_ARCHITECTURES="arm64;x86_64" \
            -DCMAKE_OSX_DEPLOYMENT_TARGET="${{ env.MACOS_DEPLOYMENT_TARGET }}"
          cmake --build .

      - name: verify universal binary
        run: |
          set -euo pipefail
          BIN="${{ env.PRODUCT_NAME }}/${{ env.PRODUCT_NAME }}-test/Plugins/${{ env.PRODUCT_NAME }}.bundle/Contents/MacOS/${{ env.PRODUCT_NAME }}"
          lipo -info "$BIN"
          lipo -info "$BIN" | grep -q "arm64" && lipo -info "$BIN" | grep -q "x86_64"

      - name: merge Windows binaries into the bundle
        run: |
          set -euo pipefail
          BUNDLE="${{ env.PRODUCT_NAME }}/${{ env.PRODUCT_NAME }}-test/Plugins/${{ env.PRODUCT_NAME }}.bundle"
          mkdir -p "$BUNDLE/Contents/Windows64"
          cp "winbuild/${{ env.PRODUCT_NAME }}.4DX" "$BUNDLE/Contents/Windows64/${{ env.PRODUCT_NAME }}.4DX"
          echo "BUNDLE_PATH=$BUNDLE" >> "$GITHUB_ENV"

      - name: codesign
        run: |
          set -euo pipefail
          IDENTITY=$(security find-identity -v -p codesigning "$KEYCHAIN_PATH" \
            | grep "Developer ID Application" | head -1 | sed -E 's/.*"(.*)".*/\1/')
          echo "Signing with: $IDENTITY"
          codesign --verbose --deep --timestamp --force --options=runtime \
            --sign "$IDENTITY" "$BUNDLE_PATH"

      - name: package, notarize & staple
        run: |
          set -euo pipefail
          VERSION="${{ needs.version.outputs.version }}"
          ZIP_PATH="${{ env.PRODUCT_NAME }}-${VERSION}.zip"
          DMG_PATH="${{ env.PRODUCT_NAME }}-${VERSION}.dmg"

          ditto -c -k --keepParent "$BUNDLE_PATH" "$ZIP_PATH"
          hdiutil create -format UDBZ -volname "${{ env.PRODUCT_NAME }}" -srcfolder "$BUNDLE_PATH" "$DMG_PATH"

          xcrun notarytool submit "$ZIP_PATH" --keychain-profile "notarytool-profile" --keychain "$KEYCHAIN_PATH" --wait
          xcrun notarytool submit "$DMG_PATH" --keychain-profile "notarytool-profile" --keychain "$KEYCHAIN_PATH" --wait
          xcrun stapler staple "$DMG_PATH"

          echo "ZIP_PATH=$ZIP_PATH" >> "$GITHUB_ENV"
          echo "DMG_PATH=$DMG_PATH" >> "$GITHUB_ENV"

      - name: create GitHub Release
        uses: softprops/action-gh-release@v2
        with:
          tag_name: ${{ needs.version.outputs.tag }}
          name: ${{ needs.version.outputs.tag }}
          files: |
            ${{ env.ZIP_PATH }}
            ${{ env.DMG_PATH }}
```

### Required GitHub Secrets for Release

The `release.yml` workflow requires the following repository secrets configured in **Settings → Secrets and variables → Actions**:

| Secret | Description |
|---|---|
| `APPLE_DEVELOPER_ID_CERTIFICATE` | Base64-encoded `.p12` Developer ID Application certificate. Export from Keychain Access, then encode: `base64 -i certificate.p12 \| pbcopy` |
| `APPLE_DEVELOPER_ID_CERTIFICATE_PASSWORD` | Password used when exporting the `.p12` file |
| `KEYCHAIN_PASSWORD` | Arbitrary password for the temporary keychain created on the runner (can be any random string) |
| `NOTARYTOOL_APPLE_ID` | Your Apple ID email address (used for notarization) |
| `NOTARYTOOL_TEAM_ID` | Your Apple Developer Team ID (10-character alphanumeric, visible in Apple Developer portal) |
| `NOTARYTOOL_PASSWORD` | App-specific password generated at [appleid.apple.com](https://appleid.apple.com/account/manage) → Sign-In and Security → App-Specific Passwords |

**Note:** The `test.yml` and `bump-version.yml` workflows do NOT require any secrets. Only `release.yml` needs them for code signing and notarization.

### CI Caveats

- **`fail-fast: false`** — run both platforms independently so you see all failures
- **CMake on Windows** — use `pwsh` shell; do NOT hardcode `-G "Visual Studio 17 2022"` — use `-A x64` alone so CMake auto-detects the installed VS version (runners update frequently)
- **Universal binary** — pass `-DCMAKE_OSX_ARCHITECTURES="arm64;x86_64"` for release builds
- **Submodules** — use `submodules: recursive` and `fetch-depth: 0` in checkout action. Shallow clones (`--depth=1`) can fail to materialize files in submodule subdirectories (especially on Windows).
- **Version management** — `bump-version.yml` reads/writes a `VERSION` file, not project files
- **Codesign verify** — do NOT add a `codesign --verify` step after signing. macOS 26 runners produce spurious "No such process" errors on verify even when the signature is valid. Notarization is the real gate — it rejects improperly signed code.

---

## README Template

Generate a `README.md` in the project root with the following structure. Replace `{name}`, `{description}`, command signatures, and examples with the actual plugin details.

````markdown
# {name}

{description}

## Requirements

- 4D v21.1 or later (compatible with the `compatibilityVersion` in `.4DProject`)

## Installation

Download the latest release from the [Releases](../../releases) page.

### macOS & Windows (single download)

1. Download the `.zip` from the release
2. Extract to get the `{name}.bundle` folder
3. Copy the `.bundle` into your 4D application's **Plugins** folder (or your database's **Plugins** folder)
4. Restart 4D

### macOS only (notarized DMG)

1. Download the `.dmg` from the release
2. Mount it and copy the `.bundle` into your **Plugins** folder
3. Restart 4D

## Commands

### `{CommandName}`

```4d
// Syntax from manifest.json, e.g.:
$result:={CommandName}($arg1; $arg2)
```

| Parameter | Type | Description |
|---|---|---|
| `$arg1` | Text | Description of first argument |
| `$result` | Text | Description of return value |

**Example:**

```4d
$result:={CommandName}("example input")
ASSERT($result="expected output")
```

{repeat for each command}

## Constants

{if the plugin defines constants in constants.xlf, list them here}

| Constant | Type | Value | Description |
|---|---|---|---|
| `Constant Name` | Longint | 1 | Description |

## Building from Source

### Prerequisites

- CMake 3.20+
- Xcode (macOS) or Visual Studio 2022 (Windows)

### Clone

```bash
git clone --recurse-submodules https://github.com/{owner}/{repo}.git
cd {repo}
```

### Build (macOS)

```bash
cd {name}
mkdir -p cmake-build && cd cmake-build
cmake .. -DCMAKE_BUILD_TYPE=Release
cmake --build .
```

### Build (Windows)

```pwsh
cd {name}
mkdir cmake-build; cd cmake-build
cmake .. -A x64
cmake --build . --config Release
```

### Run Tests

Requires [tool4d](https://developer.4d.com/docs/Admin/cli/) (free, no license needed):

```bash
/path/to/tool4d --dataless --startup-method=test_all --project=$(pwd)/{name}-test/Project/{name}.4DProject
```

## CI/CD

This project uses GitHub Actions for automated testing and releases:

| Workflow | Trigger | Purpose |
|---|---|---|
| `test.yml` | Tag push / manual | Build & test on macOS + Windows |
| `bump-version.yml` | Manual | Bump `VERSION`, commit, push tag |
| `release.yml` | `v*.*.*` tag | Build, sign, notarize, GitHub Release |

### Required Secrets (for `release.yml` only)

Configure these in **Settings → Secrets and variables → Actions**:

| Secret | Description |
|---|---|
| `APPLE_DEVELOPER_ID_CERTIFICATE` | Base64-encoded `.p12` Developer ID Application certificate |
| `APPLE_DEVELOPER_ID_CERTIFICATE_PASSWORD` | Password for the `.p12` export |
| `KEYCHAIN_PASSWORD` | Arbitrary password for the CI runner's temporary keychain |
| `NOTARYTOOL_APPLE_ID` | Apple ID email for notarization |
| `NOTARYTOOL_TEAM_ID` | Apple Developer Team ID (10-char alphanumeric) |
| `NOTARYTOOL_PASSWORD` | App-specific password from [appleid.apple.com](https://appleid.apple.com/account/manage) |

## License

{license}
````

---

## Common Pitfalls

### 1. Plugin not recognized by 4D (macOS)

**Symptom**: `nm -g` shows only `_PluginMain`, missing `_FourDPackex` and `_gCall4D`.

**Cause**: `4DPluginAPI.c` is not being compiled. The SDK's `PBXFileSystemSynchronizedRootGroup` is not associated with the build target.

**Fix**: Add `fileSystemSynchronizedGroups` to the `PBXNativeTarget` section of the pbxproj.

### 2. Linker errors for CoreGraphics (macOS)

**Symptom**: `Undefined symbols: _CGContextScaleCTM, _CGContextTranslateCTM`

**Cause**: SDK uses CoreGraphics functions in `PA_UseQuartzAxis` / `PA_UseQuickdrawAxis`.

**Fix**: Link `CoreGraphics.framework` in the Xcode project.

### 3. `localtime` deprecation (Windows)

**Symptom**: MSVC error: `'localtime': This function or variable may be unsafe`

**Fix**: Use `localtime_s` on Windows with `#ifdef _WIN32`.

### 4. `msbuild` not found in CI

**Symptom**: `msbuild: command not found` in GitHub Actions.

**Cause**: `microsoft/setup-msbuild` adds msbuild to PATH for PowerShell, not bash.

**Fix**: Use `shell: pwsh` for the Windows build step.

### 5. Duplicate plugin names

**Symptom**: Plugin not loaded, no error message.

**Cause**: Another plugin with the same `name` in manifest.json is installed.

**Fix**: Ensure the `name` field is unique.

### 6. wchar_t size mismatch

**Pitfall**: Using `L"string"` for `PA_Unichar` buffers.

**Why**: `PA_Unichar` is `unsigned short` (16-bit). On macOS, `wchar_t` is 32-bit. `L"string"` produces 32-bit characters on macOS.

**Fix**: Widen ASCII manually: `result[i] = (PA_Unichar)ascii_char;`

### 7. CRT mismatch with third-party libraries (Windows)

**Symptom**: `error LNK2038: mismatch detected for 'RuntimeLibrary': value 'MD_DynamicRelease' doesn't match value 'MT_StaticRelease'`

**Cause**: The plugin uses static CRT (`/MT`) but a third-party library added via `add_subdirectory` defaults to dynamic CRT (`/MD`).

**Fix**: Set `CMAKE_MSVC_RUNTIME_LIBRARY` globally **before** `add_subdirectory` so all targets use the same CRT:

```cmake
if(WIN32)
    set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>" CACHE STRING "" FORCE)
endif()
```

### 8. CMake generator mismatch on CI (Windows)

**Symptom**: `Visual Studio 17 2022 could not find any instance of Visual Studio`

**Cause**: GitHub runner images update frequently; `windows-latest` may ship VS 2026 while the workflow hardcodes VS 2022.

**Fix**: Omit `-G` and use only `-A x64`. CMake auto-detects the installed VS version:

```cmake
cmake .. -A x64
```

### 9. Plugin loads but "License or privilege error" (macOS)

**Symptom**: `tool4d.4DRT [-9949] License or privilege error. (PluginName (1.0))`

**Cause**: CMake's default Info.plist sets `CFBundlePackageType` to `APPL`. 4D requires `BNDL`.

**Fix**: Create `Info.plist.in` with `<string>BNDL</string>` and reference it in CMakeLists.txt:
```cmake
MACOSX_BUNDLE_INFO_PLIST "${CMAKE_SOURCE_DIR}/Info.plist.in"
```

### 10. Plugin command gets file path but can't open file (macOS)

**Symptom**: `std::ifstream(path)` fails even though 4D's `Test path name` says the file exists.

**Cause**: 4D's `Get 4D folder` returns HFS-style paths (`:` separated). C/C++ `fopen`/`ifstream` need POSIX paths (`/` separated).

**Fix**: In the 4D test method, wrap paths with `Convert path system to POSIX()`:
```4d
$path:=Convert path system to POSIX(Get 4D folder(Current resources folder))+"file.html"
```

### 11. Third-party library fails to compile on MSVC with `constexpr` errors (C3615)

**Symptom**: MSVC error `C3615: constexpr function '...' cannot result in a constant expression` in a third-party header (e.g., litehtml's `pixel_type.h` using `std::abs` in `constexpr` operators).

**Cause**: The library marks functions as `constexpr` that call standard library functions (like `std::abs`) which MSVC does not consider `constexpr`, even in C++20. The C++ standard only requires `constexpr` math functions in C++23.

**What does NOT work**:
- `set_target_properties(lib PROPERTIES CXX_STANDARD 20)` — MSVC still rejects `std::abs` in constexpr context
- `target_compile_options(lib PRIVATE /std:c++20)` — conflicts with the `/std:c++17` flag from `CXX_STANDARD` property

**Fix**: Patch the offending header at CMake configure time, replacing `constexpr` with `inline`:
```cmake
add_subdirectory("${LITEHTML_DIR}" "${CMAKE_BINARY_DIR}/litehtml")
if(MSVC)
    file(READ "${LITEHTML_DIR}/include/litehtml/pixel_type.h" _pixel_src)
    string(REPLACE "constexpr bool operator==" "inline bool operator==" _pixel_src "${_pixel_src}")
    string(REPLACE "constexpr bool operator!=" "inline bool operator!=" _pixel_src "${_pixel_src}")
    string(REPLACE "constexpr bool operator<"  "inline bool operator<"  _pixel_src "${_pixel_src}")
    string(REPLACE "constexpr bool operator>"  "inline bool operator>"  _pixel_src "${_pixel_src}")
    string(REPLACE "constexpr bool operator<=" "inline bool operator<=" _pixel_src "${_pixel_src}")
    string(REPLACE "constexpr bool operator>=" "inline bool operator>=" _pixel_src "${_pixel_src}")
    file(WRITE "${LITEHTML_DIR}/include/litehtml/pixel_type.h" "${_pixel_src}")
endif()
```

This approach is generalizable: when a third-party header uses `constexpr` with standard library functions that MSVC rejects, patch the header with `file(READ)` / `string(REPLACE)` / `file(WRITE)` in CMake.

### 12. Windows link errors with static third-party library (`__declspec(dllimport)`)

**Symptom**: Linker warnings or errors about importing symbols that should be statically linked (e.g., unresolved `__imp_archive_*` symbols).

**Cause**: Many libraries (e.g., libarchive, libcurl) auto-detect DLL vs static via a preprocessor define. Their header uses `__declspec(dllimport)` by default on Windows, assuming you link the shared library.

**Fix**: Define the library's `*_STATIC` macro on your plugin target:
```cmake
if(WIN32)
    target_compile_definitions(${PLUGIN_NAME} PRIVATE LIBARCHIVE_STATIC)
endif()
```

Common examples: `LIBARCHIVE_STATIC`, `CURL_STATICLIB`, `LIBXML_STATIC`, `PCRE2_STATIC`.

### 13. Minimizing third-party library build scope

When integrating a large library (like libarchive, libcurl, etc.) via `add_subdirectory`, disable all optional features you don't need **before** the `add_subdirectory` call:

```cmake
# Example: libarchive — disable tools, tests, and optional dependencies
set(BUILD_SHARED_LIBS OFF CACHE BOOL "" FORCE)
set(ENABLE_TAR OFF CACHE BOOL "" FORCE)
set(ENABLE_CPIO OFF CACHE BOOL "" FORCE)
set(ENABLE_CAT OFF CACHE BOOL "" FORCE)
set(ENABLE_TEST OFF CACHE BOOL "" FORCE)
set(ENABLE_INSTALL OFF CACHE BOOL "" FORCE)
# Disable optional deps that would require external packages:
set(ENABLE_OPENSSL OFF CACHE BOOL "" FORCE)
set(ENABLE_LZMA OFF CACHE BOOL "" FORCE)
add_subdirectory("${LIB_DIR}" "${CMAKE_BINARY_DIR}/libname")
```

This reduces build time, avoids missing-dependency errors on CI, and keeps the plugin self-contained. Only enable what you actually use.

### 14. Do NOT dispose returned pictures

`PA_ReturnPicture` takes ownership of the `PA_Picture`. Calling `PA_DisposePicture` after `PA_ReturnPicture` causes a **use-after-free crash** deep in the 4D runtime (`VPicture::FromVPicture_Retain`). The same applies to other `PA_Return*` functions that take opaque handles — check whether the runtime takes ownership before disposing.

```c
// WRONG — crashes at runtime
PA_Picture pic = PA_CreatePicture(data, size);
PA_ReturnPicture(params, pic);
PA_DisposePicture(pic);  // use-after-free!

// CORRECT
PA_Picture pic = PA_CreatePicture(data, size);
PA_ReturnPicture(params, pic);
// runtime owns it now — do not dispose
```

### 15. PA_JsonParse SDK bug — crashes with eVK_Object

`PA_JsonParse` in the SDK has a bug: when called with `eVK_Object`, it does **not** zero-initialize the `PA_Variable params[2]` array before calling `PA_SetStringVariable` / `PA_SetLongintVariable`. The uninitialized memory causes a crash in `GetLongOptParam` → `do_JSONParse`. The `eVK_ArrayObject` branch works because it calls `PA_CreateVariable` which zeroes the struct.

**Workaround:** bypass `PA_JsonParse` and call `PA_ExecuteCommandByID(1218, ...)` directly with `memset`-zeroed params:

```c
PA_Unistring jsonUstr = PA_CreateUnistring((PA_Unichar*)json16buf.data());

PA_Variable cmdParams[2];
memset(cmdParams, 0, sizeof(cmdParams));
PA_SetStringVariable(&cmdParams[0], &jsonUstr);
PA_SetLongintVariable(&cmdParams[1], eVK_Object);
PA_Variable result = PA_ExecuteCommandByID(1218, cmdParams, 2); // JSON Parse

PA_ReturnObject(params, PA_GetObjectVariable(result));
```

**Do NOT** call `PA_DisposeUnistring` on the string after passing it to JSON Parse — the runtime takes ownership.

### 16. macOS case-insensitive filesystem — submodule naming

On HFS+/APFS (default case-insensitive), directory names like `CLD2/` and `cld2/` refer to the **same** directory. If you name your plugin folder `CLD2` and add a git submodule at `cld2`, removing one destroys the other. **Fix:** use an unambiguous submodule name (e.g., `cld2-src`).

### 17. manifest.json object return type is `J`, not `O`

In 4D manifest syntax, the return type codes are:
- `T` — Text
- `L` — Longint / Integer
- `R` — Real
- `B` — Boolean
- `J` — Object (JSON)
- `C` — Collection
- `P` — Picture
- `O` — **not a valid return type for object** (common mistake)

Always use `:J` in the syntax string for commands that return an object.

### 18. Windows cross-platform build issues

**a) `__attribute__((visibility("default")))` is GCC/Clang only.**
MSVC does not support it. Use a conditional macro:
```cpp
#if defined(_WIN32)
#define PLUGIN_EXPORT
#else
#define PLUGIN_EXPORT __attribute__((visibility("default")))
#endif

extern "C" PLUGIN_EXPORT
void PluginMain(PA_long32 selector, PA_PluginParameters params) { ... }
```

**b) `__declspec(dllexport)` conflicts with the SDK's `PluginMain` declaration.**
The SDK header already declares `PluginMain` as `extern "C"`. Adding `__declspec(dllexport)` causes C2375 (redefinition with different linkage). **Use a `.def` file instead:**
```
EXPORTS
    PluginMain
    FourDPackex
```
And in CMakeLists.txt:
```cmake
set_target_properties(${PROJECT_NAME} PROPERTIES
    LINK_FLAGS "/DEF:${CMAKE_CURRENT_SOURCE_DIR}/${PROJECT_NAME}.def"
)
```

**c) `-Wno-*` flags are invalid on MSVC.**
Use `/wd` equivalents: `/wd4267` (size_t to int), `/wd4244` (narrowing), `/wd4305` (truncation).

**d) `TARGET_BUNDLE_DIR` is macOS-only.**
Wrap all bundle-related CMake logic in `if(APPLE)`.

**e) Windows plugin folder structure:**
```
Plugins/
  PluginName/
    Contents/
      Windows64/
        PluginName.4DX
      Resources/
        manifest.json
```
This mirrors the macOS `.bundle` structure but as a plain folder hierarchy.

### 19. catalog.4DCatalog must not be empty

A minimal catalog with no tables must still contain the base element with `name`, `uuid`, `collation_locale`, a `DEFAULT_SCHEMA`, and `journal_file` settings:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE base SYSTEM "http://www.4d.com/dtd/2007/base.dtd" >
<base name="MyPlugin" uuid="012A4E2504C248BCB748603FA468B03B" collation_locale="en-gb">
	<schema name="DEFAULT_SCHEMA"/>
	<base_extra>
		<journal_file journal_file_enabled="false"/>
	</base_extra>
</base>
```

Note: the DTD is `base.dtd`, **not** `catalog_V1.0.dtd`. Generate a unique UUID for each project.

### 20. Homebrew libraries are single-architecture

Homebrew on Apple Silicon installs arm64-only libraries. When building a universal plugin (`x86_64;arm64`) that links a Homebrew library (e.g., PoDoFo), the linker will fail for x86_64. Solutions:
- Build arm64-only for local testing
- Build the library from source as universal for CI/release
- Use a submodule with `ExternalProject_Add` in CMake

### 21. SDK `4DPluginAPI.c` compilation warnings/errors

The SDK's `4DPluginAPI.c` may produce warnings or errors on modern compilers:
- `sLONG_PTR result = NULL;` causes `-Wint-conversion` error (pointer-to-integer)
- Multi-character constants `'OK'` and `'NO'` cause `-Wmultichar` warnings

Suppress these per-file in CMakeLists.txt:
```cmake
if(APPLE)
    set_source_files_properties("${SDK_DIR}/4DPluginAPI.c" PROPERTIES
        COMPILE_FLAGS "-Wno-int-conversion -Wno-multichar")
endif()
```

### 22. `PA_GetUnistringPtr` does not exist

The correct function name is `PA_GetUnistring` (returns `PA_Unichar*`). There is no `PA_GetUnistringPtr` in the SDK. The length function is `PA_GetUnistringLength`.

---

## Step-by-Step Checklist

Given a plugin specification (name, commands, constants, behavior):

### 1. Repository Setup
- [ ] Create repository
- [ ] Add `4D-Plugin-SDK` as git submodule
- [ ] Add any third-party library dependencies as git submodules
- [ ] Create `VERSION` file with initial version `1.0.0`
- [ ] Create `.gitignore`
- [ ] Commit

### 2. Project Files
- [ ] Create `{name}/` directory
- [ ] Create `{name}-4dplugin.h` with function declarations
- [ ] Create `{name}-4dplugin.cpp` with `PluginMain` stub and empty command functions
- [ ] Create `manifest.json` with command syntax
- [ ] Create `constants.xlf` with constant definitions

### 3. CMakeLists.txt and Info.plist.in
- [ ] Create `{name}/Info.plist.in` with `CFBundlePackageType = BNDL`
- [ ] Create `{name}/CMakeLists.txt` using the template (with `MACOSX_BUNDLE_INFO_PLIST`)
- [ ] Add third-party library via `add_subdirectory` if applicable
- [ ] Set include directories for third-party headers
- [ ] Link third-party libraries to the plugin target

### 4. 4D Test Project
- [ ] Create blank 4D project in `{name}-test/`
- [ ] Set `compatibilityVersion` in `.4DProject`
- [ ] Create `folders.json` with "Tests" virtual folder
- [ ] Create test methods with ASSERT statements
- [ ] Create `test_all.4dm` runner method
- [ ] Run tests locally with tool4d

### 5. C/C++ Implementation
- [ ] Implement command functions using SDK getters/setters
- [ ] Handle platform differences with `#ifdef _WIN32`
- [ ] Build on both platforms (or at least macOS for initial validation)
- [ ] Run tests

### 6. GitHub Actions Workflows
- [ ] Create `.github/workflows/test.yml` (CMake build + tool4d tests)
- [ ] Create `.github/workflows/bump-version.yml` (version bump + tag)
- [ ] Create `.github/workflows/release.yml` (build, sign, notarize, release)
- [ ] Verify CI passes

### 7. Documentation
- [ ] Create `README.md` using the template (see [README Template](#readme-template))
- [ ] Document required GitHub Secrets for release workflow

---

## Reference: Reusable GitHub Action for tool4d

For more complex setups, see: https://github.com/miyako/4D/blob/v1/.github/actions/tool4d-download/action.yml

This action handles:
- Platform detection (Windows/macOS)
- Architecture selection (x86_64/arm64)
- Version-specific URL formatting
- Download and extraction

---

## CMake-Based Project Generation (Recommended for Automation)

Instead of manually crafting Xcode `.pbxproj` and Visual Studio `.vcxproj` files, use CMake to generate both from a single `CMakeLists.txt`. This is the **recommended approach for agents** since CMake files are plain text and easy to generate programmatically.

### Info.plist.in Template (CRITICAL)

Create `{name}/Info.plist.in` — this is **required** for the plugin to be recognized by 4D. Without it, CMake defaults `CFBundlePackageType` to `APPL` and 4D will refuse to load the plugin with a "License or privilege error".

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleDevelopmentRegion</key>
    <string>English</string>
    <key>CFBundleExecutable</key>
    <string>${MACOSX_BUNDLE_EXECUTABLE_NAME}</string>
    <key>CFBundleIdentifier</key>
    <string>${MACOSX_BUNDLE_GUI_IDENTIFIER}</string>
    <key>CFBundleInfoDictionaryVersion</key>
    <string>6.0</string>
    <key>CFBundlePackageType</key>
    <string>BNDL</string>
    <key>CFBundleShortVersionString</key>
    <string>${MACOSX_BUNDLE_SHORT_VERSION_STRING}</string>
    <key>CFBundleVersion</key>
    <string>${MACOSX_BUNDLE_BUNDLE_VERSION}</string>
</dict>
</plist>
```

### CMakeLists.txt Template

Place this in the `{name}/` directory alongside the source files:

```cmake
cmake_minimum_required(VERSION 3.20)

# Set the plugin name — change this for your project
set(PLUGIN_NAME "{name}" CACHE STRING "Plugin name")

project(${PLUGIN_NAME} LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_C_STANDARD 17)

# --- SDK paths ---
set(SDK_DIR "${CMAKE_SOURCE_DIR}/../4D-Plugin-SDK/4D Plugin API")

# --- Static CRT on Windows (MUST be set before add_subdirectory for third-party libs) ---
if(WIN32)
    set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>" CACHE STRING "" FORCE)
endif()

# --- Source files ---
set(PLUGIN_SOURCES
    ${PLUGIN_NAME}-4dplugin.cpp
    "${SDK_DIR}/4DPluginAPI.c"
)

set(PLUGIN_HEADERS
    ${PLUGIN_NAME}-4dplugin.h
    "${SDK_DIR}/4DPluginAPI.h"
    "${SDK_DIR}/EntryPoints.h"
    "${SDK_DIR}/PublicTypes.h"
    "${SDK_DIR}/PrivateTypes.h"
    "${SDK_DIR}/Flags.h"
)

set(PLUGIN_RESOURCES
    manifest.json
    constants.xlf
)

# --- Platform-specific target setup ---
if(APPLE)
    # macOS: create a .bundle
    add_library(${PLUGIN_NAME} MODULE ${PLUGIN_SOURCES} ${PLUGIN_HEADERS} ${PLUGIN_RESOURCES})

    set_target_properties(${PLUGIN_NAME} PROPERTIES
        BUNDLE TRUE
        BUNDLE_EXTENSION "bundle"
        MACOSX_BUNDLE_INFO_PLIST "${CMAKE_SOURCE_DIR}/Info.plist.in"
        MACOSX_BUNDLE_GUI_IDENTIFIER "com.4d.${PLUGIN_NAME}"
        MACOSX_BUNDLE_BUNDLE_VERSION "1.0"
        MACOSX_BUNDLE_SHORT_VERSION_STRING "1.0"
    )

    # Copy resources into the bundle
    set_source_files_properties(${PLUGIN_RESOURCES} PROPERTIES
        MACOSX_PACKAGE_LOCATION Resources
    )

    # Link CoreGraphics (required by SDK for Quartz axis functions)
    find_library(COREGRAPHICS_FRAMEWORK CoreGraphics REQUIRED)
    target_link_libraries(${PLUGIN_NAME} PRIVATE ${COREGRAPHICS_FRAMEWORK})

    # Output to test project Plugins folder
    set_target_properties(${PLUGIN_NAME} PROPERTIES
        LIBRARY_OUTPUT_DIRECTORY "${CMAKE_SOURCE_DIR}/${PLUGIN_NAME}-test/Plugins"
        LIBRARY_OUTPUT_DIRECTORY_DEBUG "${CMAKE_SOURCE_DIR}/${PLUGIN_NAME}-test/Plugins"
        LIBRARY_OUTPUT_DIRECTORY_RELEASE "${CMAKE_SOURCE_DIR}/${PLUGIN_NAME}-test/Plugins"
    )

elseif(WIN32)
    # Windows: create a DLL with .4DX extension
    add_library(${PLUGIN_NAME} SHARED ${PLUGIN_SOURCES} ${PLUGIN_HEADERS})

    set_target_properties(${PLUGIN_NAME} PROPERTIES
        SUFFIX ".4DX"
        # Module definition file exports FourDPackex
        LINK_FLAGS "/DEF:\"${SDK_DIR}/4DPluginAPI.def\""
    )

    # Static CRT linkage
    set_property(TARGET ${PLUGIN_NAME} PROPERTY
        MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>"
    )

    # Output to test project Plugins folder (Windows structure)
    set(PLUGIN_OUT_DIR "${CMAKE_SOURCE_DIR}/${PLUGIN_NAME}-test/Plugins/${PLUGIN_NAME}/Contents/Windows64")
    set(RESOURCE_OUT_DIR "${CMAKE_SOURCE_DIR}/${PLUGIN_NAME}-test/Plugins/${PLUGIN_NAME}/Contents/Resources")

    set_target_properties(${PLUGIN_NAME} PROPERTIES
        RUNTIME_OUTPUT_DIRECTORY "${PLUGIN_OUT_DIR}"
        RUNTIME_OUTPUT_DIRECTORY_DEBUG "${PLUGIN_OUT_DIR}"
        RUNTIME_OUTPUT_DIRECTORY_RELEASE "${PLUGIN_OUT_DIR}"
    )

    # Post-build: copy resources
    add_custom_command(TARGET ${PLUGIN_NAME} POST_BUILD
        COMMAND ${CMAKE_COMMAND} -E make_directory "${RESOURCE_OUT_DIR}"
        COMMAND ${CMAKE_COMMAND} -E copy_if_different
            "${CMAKE_SOURCE_DIR}/manifest.json"
            "${CMAKE_SOURCE_DIR}/constants.xlf"
            "${RESOURCE_OUT_DIR}"
        COMMENT "Copying manifest.json and constants.xlf to Resources"
    )
endif()

# --- Common settings ---
target_include_directories(${PLUGIN_NAME} PRIVATE "${SDK_DIR}")

# Compile 4DPluginAPI.c as C++
set_source_files_properties("${SDK_DIR}/4DPluginAPI.c" PROPERTIES LANGUAGE CXX)

# Unicode charset
target_compile_definitions(${PLUGIN_NAME} PRIVATE UNICODE _UNICODE)
```

### Building with CMake

**macOS:**
```bash
cd {name}
mkdir -p cmake-build && cd cmake-build
cmake .. -DCMAKE_BUILD_TYPE=Debug
cmake --build .
```

**Windows:**
```pwsh
cd {name}
mkdir cmake-build; cd cmake-build
cmake .. -A x64
cmake --build . --config Debug
```

### CI/CD with CMake

See the full workflow templates in [GitHub Actions CI/CD](#github-actions-cicd) and [GitHub Actions — Release Workflow](#github-actions--release-workflow). CMake eliminates the need for `microsoft/setup-msbuild` since it finds the compiler automatically.

### Advantages for Automation

- Single `CMakeLists.txt` generates both platform projects
- Plain text format — easy to generate and modify programmatically
- No need to manage Xcode object IDs or MSBuild XML
- Adding source files requires editing one place, not two project files
- Cross-platform build commands are simple and well-documented

---

## Worked Example: CLD2 (Compact Language Detector 2) Plugin

This section documents a complete plugin built from scratch wrapping the [CLD2 library](https://github.com/CLD2Owners/cld2).

### Overview

- **Syntax:** `result:=CLD2(text)` — takes a text string, returns a JSON object with detected languages
- **Return value:** Object containing `language`, `language_code`, `is_reliable`, `text_bytes`, and a `candidates` array with top-3 language guesses (each with name, code, percent, score)
- **Plugin ID:** 31000, Command ID: 1

### Repository Structure

```
4d-plugin-CLD2/
├── .github/workflows/
│   ├── test.yml          # CI: build + test with tool4d
│   ├── release.yml       # Build, sign, notarize, create release
│   └── bump-version.yml  # Bump VERSION file
├── CLD2/                 # Plugin source folder
│   ├── CMakeLists.txt
│   ├── CLD2-4dplugin.cpp
│   ├── manifest.json
│   ├── Info.plist
│   └── CLD2-test/        # 4D test project
│       ├── Plugins/      # Pre-built plugin for testing
│       └── Project/
├── 4D-Plugin-SDK/        # Submodule
├── cld2-src/             # Submodule (NOT "cld2" — see Pitfall #16)
├── README.md
└── VERSION
```

### Key Implementation Details

**CLD2 API usage:**
```cpp
#include "compact_lang_det.h"
#include "lang_script.h"  // for LanguageCode(), LanguageName()

CLD2::Language language3[3];
int percent3[3];
double normalized_score3[3];
int text_bytes;
bool is_reliable;

CLD2::Language lang = CLD2::DetectLanguageSummary(
    utf8text, length, true,
    language3, percent3, normalized_score3,
    nullptr, &text_bytes, &is_reliable);
```

**JSON construction pattern:**
Build a UTF-8 JSON string manually (snprintf or string concatenation), convert to UTF-16, then use the `PA_ExecuteCommandByID(1218)` workaround (see Pitfall #15) to parse it into a 4D object.

**CMakeLists.txt notes:**
- SDK path: `${CMAKE_CURRENT_SOURCE_DIR}/../4D-Plugin-SDK/4D Plugin API`
- CLD2 sources: `${CMAKE_CURRENT_SOURCE_DIR}/../cld2-src/internal/*.cc` (full variant tables)
- Suppress CLD2 warnings: `-Wno-c++11-narrowing -Wno-writable-strings -Wno-unused-variable -Wno-tautological-undefined-compare`
- Post-build copy for `manifest.json`:

```cmake
set(PLUGIN_OUTPUT "${CMAKE_BINARY_DIR}/${PROJECT_NAME}.bundle")
add_custom_command(TARGET ${PROJECT_NAME} POST_BUILD
    COMMAND ${CMAKE_COMMAND} -E make_directory "${PLUGIN_OUTPUT}/Contents/Resources"
    COMMAND ${CMAKE_COMMAND} -E copy
        "${CMAKE_CURRENT_SOURCE_DIR}/manifest.json"
        "${PLUGIN_OUTPUT}/Contents/Resources/manifest.json"
)
```

### Lessons Learned

1. **Submodule naming on macOS** — never use a name that differs only in case from your plugin folder (Pitfall #16)
2. **PA_JsonParse is buggy** — use direct `PA_ExecuteCommandByID(1218)` with zeroed params (Pitfall #15)
3. **Don't dispose strings passed to the runtime** — same ownership pattern as pictures (Pitfall #14)
4. **Object return type in manifest is `J`** — not `O` (Pitfall #17)
5. **Always use `PA_GetObjectVariable(result)`** — not `result.uValue.fObject` directly
6. **Deploy both binary AND manifest.json** to the test project's Plugins folder after builds
7. **Windows needs a `.def` file** for exports — `__declspec(dllexport)` conflicts with SDK header (Pitfall #18)
8. **Wrap all macOS-specific CMake** in `if(APPLE)` — frameworks, bundle properties, visibility flags (Pitfall #18)

---

## Worked Example: PDF to PDF/A-3 (PoDoFo) Plugin

This section documents a plugin that converts PDF to PDF/A-3 for Factur-X / ZUGFeRD compliance.

### Background: Factur-X and PDF/A-3

Factur-X is a Franco-German hybrid e-invoice standard (identical to ZUGFeRD 2.x). It combines a human-readable PDF with a machine-readable XML file embedded within a single PDF/A-3 document.

**PDF/A-3** (ISO 19005-3) is required because it is the only PDF/A variant that supports embedded file attachments. Converting a standard PDF to PDF/A-3 is the first step toward Factur-X compliance.

### Library Selection

Three approaches were evaluated for C/C++ PDF→PDF/A-3 conversion:

| Approach | License | Quality | Verdict |
|----------|---------|---------|---------|
| **Ghostscript C API** | AGPL ⚠️ | Excellent — battle-tested | Requires commercial license for proprietary use |
| **Ghostscript subprocess** | No linking ✅ | Excellent | Requires `gs` installed on user's machine |
| **PoDoFo** | LGPL ✅ | Sufficient for clean input | ✅ Selected — best license, works for 4D-generated PDFs |

**Key insight:** PDFs generated by 4D's printing system (Quartz on macOS, Microsoft Print to PDF on Windows) are already "clean" — embedded fonts, RGB color space, no transparency. So PoDoFo's approach of simply adding PDF/A-3 metadata and ICC OutputIntent is sufficient. Validated with **veraPDF**: 146 rules passed, 0 failed.

### Command

```
result:=PDF TO PDFA(inputPath; outputPath)
```

- Plugin ID: 32000, Command ID: 1
- Returns: `{"success": true/false, "code": 0, "message": "..."}`

### How the Conversion Works (PoDoFo)

1. Load the PDF with `PdfMemDocument::Load()`
2. Create XMP metadata stream declaring `pdfaid:part=3`, `pdfaid:conformance=B`
3. Read sRGB ICC profile from Resources, create ICC stream object with `N=3` (RGB)
4. Create OutputIntent dictionary (`GTS_PDFA1`, `sRGB IEC61966-2.1`)
5. Add OutputIntents array to catalog
6. Save with `PdfMemDocument::Save()`

### Repository Structure

```
4d-plugin-PDFA-PoDoFo/
├── .github/workflows/
│   ├── test.yml
│   ├── release.yml       # Windows first → macOS sign/notarize/release
│   └── bump-version.yml
├── PDFA/
│   ├── CMakeLists.txt    # C++17, pkg-config for PoDoFo
│   ├── PDFA-4dplugin.cpp
│   ├── manifest.json
│   ├── Info.plist
│   ├── PDFA.def
│   ├── resources/
│   │   └── sRGB.icc      # Bundled ICC color profile
│   └── PDFA-test/
│       ├── Plugins/PDFA.bundle/
│       └── Project/
├── 4D-Plugin-SDK/         # Submodule
├── README.md, VERSION, LICENSE
```

### CMake Notes for PoDoFo

- Requires C++17: `set(CMAKE_CXX_STANDARD 17)`
- Find via pkg-config (macOS: `brew install podofo`) or vcpkg (Windows)
- Must suppress SDK warnings: `-Wno-int-conversion -Wno-multichar` (Pitfall #21)
- Homebrew PoDoFo is arm64-only — build arm64 for local testing (Pitfall #20)
- Windows: `vcpkg install podofo:x64-windows` + `-DCMAKE_TOOLCHAIN_FILE=.../vcpkg.cmake`

### ICC Color Profile

All approaches require an sRGB ICC profile. Sources:
- macOS system: `/System/Library/ColorSync/Profiles/sRGB Profile.icc`
- Free download: https://www.color.org/srgbprofiles.xalter

The profile is bundled in `Contents/Resources/sRGB.icc` and located at runtime via `CFBundleCopyResourcesDirectoryURL` (macOS) or `GetModuleFileNameA` (Windows).

### Validation

Use [veraPDF](https://verapdf.org/) to validate the output:

```bash
brew install verapdf
verapdf --flavour 3b output.pdf
```

Expected: `isCompliant="true"`, `failedRules="0"`.

### Lessons Learned

1. **PoDoFo adds metadata but doesn't fix non-compliant input** — works only because 4D's PDF output is already clean
2. **Homebrew libraries are arm64-only** on Apple Silicon — can't build universal without building from source (Pitfall #20)
3. **SDK `4DPluginAPI.c` needs warning suppression** on modern compilers (Pitfall #21)
4. **`PA_GetUnistring`** is the correct function name, not `PA_GetUnistringPtr` (Pitfall #22)
5. **catalog.4DCatalog** must have `base.dtd`, `name`, `uuid`, `DEFAULT_SCHEMA`, `journal_file` (Pitfall #19)
6. **CI workflow pattern:** Windows first → macOS downloads Windows binaries → merge into bundle → sign → notarize → release
7. **Bump-version + release:** Tags pushed by `GITHUB_TOKEN` don't trigger other workflows; push tags manually or use a PAT
8. **veraPDF** is the industry-standard open-source PDF/A validator — use it to verify output compliance

### 23. Writing output strings via pointer parameters (`&Z`)

The modern 4D Plugin API has **no `PA_SetStringParameter`**. To write a string back to the caller from a plugin command:

1. **Manifest**: declare the parameter as `&Z` (Pointer), not `&T` (Text)
2. **4D caller**: passes `->$variable` syntax
3. **Plugin code**:

```cpp
PA_Pointer ptr = PA_GetPointerParameter(params, index);
if (!ptr) return;

PA_Unistring ustr = /* your PA_Unistring */;
PA_Variable var = PA_CreateVariable(eVK_Unistring);
PA_SetStringVariable(&var, &ustr);
PA_SetPointerValue(ptr, var);
// Do NOT call PA_DisposeUnistring or PA_ClearVariable after this!
```

**Critical rules:**
- Use `eVK_Unistring` (33), NOT `eVK_Text` (2). `PA_SetStringVariable` sets `fType = eVK_Unistring`. Using the wrong type causes a crash in 4D's `copycv_plugin`.
- Use `PA_CreateVariable(eVK_Unistring)` + `PA_SetStringVariable()` — never manually construct `PA_Variable` with `memset`/field assignment.
- After `PA_SetPointerValue`, 4D **owns** the variable content (SDK comment: *"do NOT call PA_ClearVariable after this call"*). Calling `PA_DisposeUnistring` after is a use-after-free crash.
- `PA_GetVariableParameter` is for reading the variable a pointer points to — it does NOT work for `&Z` pointer params. Use `PA_GetPointerParameter` instead.

### 24. Bundling third-party dylibs (macOS Apple Silicon)

When bundling third-party dylibs (e.g., Oracle Instant Client, OpenSSL, libcurl) alongside a plugin:

1. **Copy as versioned real files** — many libraries ship as symlinks (e.g., `libfoo.dylib` → `libfoo.dylib.1.2`). CMake `copy_if_different` follows symlinks and loses the versioned name. Copy explicitly with versioned target names.
2. **Do NOT use symlinks/aliases** in the plugin bundle — causes crashes in dyld.
3. **Change `@rpath` → `@loader_path`** using `install_name_tool -id` and `-change` on ALL dylibs. This makes the library resolve relative to the plugin binary, not the application.
4. **Re-sign after `install_name_tool`** — modifying a signed dylib invalidates its code signature. Apple Silicon enforces signatures. Run `codesign -fs -` (ad-hoc) after every `install_name_tool` invocation during local development.
5. **Fix the full dependency chain** — if library A depends on library B, both must have their install names and inter-library references changed to `@loader_path`. Use `otool -L` to inspect the chain.

### 25. Native pointers exceed PA_long32

Native library handles and pointers are `void*` (8 bytes on 64-bit) but `PA_long32` is only 4 bytes. Passing raw pointers as longint parameters silently truncates the upper 32 bits. Use an internal handle table that maps small `PA_long32` IDs (1, 2, 3, ...) to actual `void*` pointers. The table should be thread-safe (`std::mutex`).

### 26. CI/CD: macOS codesigning and notarization

All plugin projects use the same GitHub Actions repository secrets for macOS codesigning and notarization:

| Secret | Description |
|--------|-------------|
| `APPLE_DEVELOPER_ID_CERTIFICATE` | Base64-encoded Developer ID Application `.p12` certificate |
| `APPLE_DEVELOPER_ID_CERTIFICATE_PASSWORD` | Password for the `.p12` file |
| `KEYCHAIN_PASSWORD` | Password for the temporary CI keychain |
| `NOTARYTOOL_APPLE_ID` | Apple ID email for `xcrun notarytool` |
| `NOTARYTOOL_PASSWORD` | App-specific password (generate at appleid.apple.com → Sign-In and Security → App-Specific Passwords) |
| `NOTARYTOOL_TEAM_ID` | 10-character Apple Developer Team ID |

**Workflow structure:** Two separate workflows:
- `build.yml` — compile + verify on every push/PR. No signing, no artifacts.
- `release.yml` — build both platforms + codesign + notarize + GitHub Release. Triggers on `v*.*.*` tags or manual `workflow_dispatch`.

**Certificate import pattern** (proven across all plugin projects):
```yaml
echo -n "$CERT_BASE64" | base64 --decode -o "$CERT_PATH"
security create-keychain -p "$KEYCHAIN_PASSWORD" "$KEYCHAIN_PATH"
security import "$CERT_PATH" -P "$CERT_PASSWORD" -A -t cert -f pkcs12 -k "$KEYCHAIN_PATH"
security list-keychain -d user -s "$KEYCHAIN_PATH" login.keychain
security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$KEYCHAIN_PASSWORD" "$KEYCHAIN_PATH"
```

**Identity discovery:** Use `security find-identity -v -p codesigning "$KEYCHAIN_PATH"` to find the signing identity dynamically. Do NOT construct it manually from the team ID.

**Notarytool credentials:** Store in the keychain with `xcrun notarytool store-credentials "notarytool-profile"`, then use `--keychain-profile "notarytool-profile" --keychain "$KEYCHAIN_PATH"` for submissions.

**Signing order (inside-out):** embedded dylibs → plugin binary → bundle. All with `--options runtime --timestamp`.

**Release artifacts:** Create both `.zip` (cross-platform bundle) and `.dmg` (mac-friendly, can be stapled). Notarize both, staple the dmg. Publish with `softprops/action-gh-release@v2`.

**Third-party dylibs:** must be re-signed with Developer ID (not ad-hoc) before notarization. Ad-hoc signatures (`codesign -fs -`) are only for local development builds.

**Cross-platform bundles and `--deep`:** 4D plugin bundles often contain both macOS (`Contents/MacOS/`) and Windows (`Contents/Windows64/`) binaries. Do NOT use `codesign --verify --deep --strict` on such bundles — `--deep` traverses into `Contents/Windows64/` and fails on non-Mach-O files (`.4DX`, `.dll`) with "No such process". Use `codesign -dvv` to display signature info instead. Notarytool will validate the signature during submission.

### 27. CI/CD: vendored third-party libraries

Large third-party binaries (e.g., Oracle Instant Client, database drivers, crypto libraries) often cannot be committed to the repo due to licensing or size. Store them as GitHub Release assets under a dedicated tag (e.g., `vendor-libs`):

- Create platform-specific tarballs (e.g., `libs-macos-aarm64.tar.gz`, `libs-windows-x64.tar.gz`)
- CI downloads them with `gh release download <tag>`. No extra secrets needed — `GITHUB_TOKEN` has read access to the repo's own releases.

**Committable artifacts** (small, no license issue):
- Header files (`.h`) — type definitions and function declarations
- Import libraries (`.lib` on Windows) — just symbol stubs, not runtime code
