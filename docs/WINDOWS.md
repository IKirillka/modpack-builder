# Установка Modpack Builder на Windows

Нужны Python 3.11 или новее, Codex, Chrome или Chromium и Prism Launcher. Установщик использует отдельное окружение Python. Нужен интернет для установки зависимостей.

## 1. Установите локальный компонент

Распакуйте ZIP версии 0.7.1 или новее. Откройте PowerShell в извлечённой папке `minecraft-mod-curator`.

```powershell
py -3 --version
py -3 -X utf8 .\scripts\install.py
py -3 -X utf8 .\scripts\launch.py --help
```

Если `py` недоступен, используйте `python` вместо `py -3` и проверьте его версию. Для пропуска установки преднастроенных агентов выполните `$env:MOD_CURATOR_SKIP_AGENTS = '1'` перед установкой.

## 2. Подключите Codex и откройте браузер

Установщик покажет путь к локальному каталогу плагинов. При стандартных настройках подключение выглядит так.

```powershell
$state = Join-Path $HOME '.local/share/mod-curator'
$market = Join-Path $state 'windows-marketplace'
codex plugin marketplace add "$market"
codex plugin add minecraft-mod-curator@minecraft-mod-tools-local
```

При обновлении старой установки проверьте путь командой `codex plugin marketplace list`. Если тот же каталог зарегистрирован с другим путём, выполните `codex plugin marketplace remove minecraft-mod-tools-local`, затем повторите две команды подключения. При заданном `MOD_CURATOR_HOME` используйте путь из сообщения установщика.

Если ваша версия Codex поддерживает только прямое подключение MCP, используйте этот вариант вместо команд регистрации плагина. Откройте новый чат и попросите Codex прочитать файл `skills/curate-minecraft-mods/SKILL.md` из распакованного пакета.

```powershell
$runtimePython = Join-Path $HOME '.local/share/mod-curator/runtime/Scripts/python.exe'
codex mcp add minecraft_mod_curator -- "$runtimePython" -X utf8 -m mod_curator serve --transport stdio
codex mcp list
```

Для нестандартной папки укажите её в аргументе `--env "MOD_CURATOR_HOME=$state"` перед `--` и используйте путь к Python из сообщения установщика.

Если компонент уже настроен для работы через браузер, используйте существующую папку экспорта и псевдоним. Для новой установки выполните команды ниже в папке плагина.

```powershell
$packFolder = Join-Path $HOME 'Minecraft/ModCuratorPacks'
py -3 -X utf8 .\scripts\launch.py init --destination "$packFolder" --alias packs --transport cdp
py -3 -X utf8 .\scripts\start_browser.py
```

Оставьте открытым отдельное окно браузера. Если CurseForge покажет проверку, пройдите её вручную в этой вкладке. Для нестандартной установки браузера передайте полный путь к его `.exe` через `--browser-executable`. Браузер доступен плагину по `127.0.0.1:9222`.

## 3. Проверьте на новой сборке

Откройте новый локальный чат и отправьте «Используй Modpack Builder. Подбери сборку для Minecraft 1.21.1 и NeoForge на основе Create. Ищи в категории Create. Порог 100 000 скачиваний. Для экспорта используй packs».

Для первого прохода достаточно выбрать несколько проектов. Попросите проверить выбранную ревизию, затем явно экспортировать её в ZIP. В Prism Launcher выберите **Добавить экземпляр → Импорт**, укажите ZIP и создайте тестовый экземпляр. Сравните список файлов с планом и проверьте, запросил ли Prism ручное скачивание. Если команда импорта из чата не находит переносной Prism, откройте ZIP через интерфейс Prism или укажите путь к `prismlauncher.exe` переменной `MOD_CURATOR_PRISM_EXECUTABLE` в окружении Codex.

Старые задания хранятся в прежней папке `%USERPROFILE%\.local\share\mod-curator` или в папке из `MOD_CURATOR_HOME`. После обновления перезапустите локальный Codex, чтобы загрузить новую конфигурацию MCP. Для автоматического запуска моделей через дочерний Codex CLI нужен доступный `codex.exe` или стандартная установка через npm с Node.js. При переносной установке задайте `MOD_CURATOR_CODEX_EXECUTABLE`.

На Windows используйте ZIP и импорт в Prism. Прямая установка JAR через плагин и окончательная очистка корзины используют файловые операции POSIX. Перемещение в корзину и восстановление доступны отдельно. Чистая установка 0.7.1 проверена через MCP на Linux. Сценарий повторного прогона на Windows входит в автоматическую сборку, а интерфейсы Codex Desktop и Prism требуют отдельного теста на Windows.
