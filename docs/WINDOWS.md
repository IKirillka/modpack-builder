# Modpack Builder на Windows

Понадобятся Python 3.11 или новее и доступ в интернет. Для работы из чата нужен локальный Codex. Для каталога используйте Chrome, Chromium или Edge, для импорта ZIP — Prism Launcher. Установка выполняется от обычного пользователя без WSL и Git Bash.

## 1. Установка или обновление

Распакуйте ZIP версии 0.8.3 в локальную папку, откройте в ней PowerShell и выполните:

```powershell
.\install.cmd
py -3 -X utf8 .\scripts\launch.py --help
```

Если `py` недоступен, `install.cmd` попробует `python`. В остальных примерах замените `py -3` на `python`. При обновлении закройте Codex и отдельно запущенный MCP. Не переносите папку окружения Python между компьютерами. Установщик использует прежнее место хранения `%USERPROFILE%\.local\share\mod-curator` или заданный `MOD_CURATOR_HOME`. В нём остаются SQLite, задания, настройки, кэш и корзина.

## 2. Подключение Codex

Возьмите путь **Codex local marketplace**, который вывел установщик. Для стандартной установки:

```powershell
$state = Join-Path $HOME '.local\share\mod-curator'
$market = Join-Path $state 'windows-marketplace'
codex plugin marketplace add "$market"
codex plugin add minecraft-mod-curator@minecraft-mod-tools-local
```

Если этот каталог уже зарегистрирован, проверьте путь в `codex plugin marketplace list`. Для перехода со старого пути сначала удалите регистрацию этого каталога через `codex plugin marketplace remove minecraft-mod-tools-local`, затем добавьте новый путь. После обновления откройте новый **локальный** чат Codex. Фоновому модельному анализу нужен доступ к авторизованному Codex CLI. Он находит `codex.exe` и npm-установку с Node.js. Для переносной установки задайте `MOD_CURATOR_CODEX_EXECUTABLE` в окружении Codex.

Если клиент поддерживает локальный MCP, а команды `codex plugin` нет, подключите MCP напрямую. Используйте только один из двух способов.

```powershell
$runtimePython = Join-Path $state 'runtime\Scripts\python.exe'
codex mcp add minecraft_mod_curator -- "$runtimePython" -X utf8 -m mod_curator serve --transport stdio
codex mcp list
```

При собственном `MOD_CURATOR_HOME` используйте путь к Python из сообщения установщика и добавьте `--env "MOD_CURATOR_HOME=$state"` перед `--`. В новом чате укажите файл `skills/curate-minecraft-mods/SKILL.md` из распакованного пакета.

## 3. Каталог и ключ CurseForge

Настройте папку ZIP при первой установке. При обновлении можно оставить прежний псевдоним и настройки браузера.

```powershell
$packFolder = Join-Path $HOME 'Minecraft\ModCuratorPacks'
py -3 -X utf8 .\scripts\launch.py init --destination "$packFolder" --alias packs --transport cdp
py -3 -X utf8 .\scripts\start_browser.py
```

Скрипт ждёт ответа браузера на `127.0.0.1:9222`. Оставьте это окно открытым и при необходимости пройдите проверку CurseForge вручную. Для переносного браузера укажите `--browser-executable 'D:\Apps\Chrome\chrome.exe'`. Если нужен другой порт, передайте `--port 9333` браузеру и `--cdp-url 'http://127.0.0.1:9333'` при `init`. После сбоя возобновите сохранённое задание по ID.

Для автономного режима Playwright установите его Chromium командой `.\install.cmd --with-chromium`, затем выберите `--transport browser`. Отдельное окно CDP полезно, если сайт требует ручной проверки.

Ключ API необязателен. Команда ниже принимает ключ в скрытом поле, проверяет доступ к проекту Minecraft и сохраняет значение в диспетчере учётных данных Windows. [Настройка CurseForge API](INSTALL.md#ключ-curseforge-api).

```powershell
py -3 -X utf8 .\scripts\launch.py curseforge-key set
py -3 -X utf8 .\scripts\launch.py curseforge-key check
```

## 4. Проверка сборки и Prism

В новом чате отправьте «Используй Modpack Builder. Подбери несколько модов для Minecraft 1.21.1 и NeoForge, категория Create, выше 100 000 скачиваний. Основа — автоматизация. Для экспорта используй packs».

Запросите предпроверку и проверьте отчёт. Затем явно запросите экспорт. В Prism выберите **Добавить экземпляр → Импорт** и укажите ZIP. Моды, которые Prism требует скачать вручную, должны совпадать по версии и файлу с планом. Для переносного Prism можно задать `MOD_CURATOR_PRISM_EXECUTABLE` полным путём к `prismlauncher.exe`.

```powershell
py -3 -X utf8 .\scripts\launch.py verify prism "ID_ЭКСПОРТА" --instance "D:\Prism\instances\Тестовая сборка"
```

Ожидайте пустые списки `missing`, `extra`, `different` и `unverified`. Для остановки, продолжения и сверки точного плана используйте [сценарий повторной проверки](P0_WINDOWS_TRIAL.md).

Прямое скачивание JAR также доступно после подтверждения. Для него зарегистрируйте отдельную папку на локальном диске. Оно сохраняет существующие файлы, проверяет хеши и помещает готовый файл атомарно. Если путь перенаправлен через junction или sync placeholder, выберите обычную локальную папку либо воспользуйтесь импортом ZIP в Prism. Для правки и отката зарегистрированного тестового экземпляра предварительно закройте игру и Prism, чтобы Windows освободила файлы.

Перемещение задания в корзину, восстановление и окончательное удаление доступны через MCP и CLI. Для проверки удаления используйте одноразовое задание. Окончательная очистка без явного включения срока хранения выключена. [Команды корзины и автоочистки](JOB_CLEANUP.md).

## Автоматическая проверка пакета

Из исходников или распакованного пакета:

```powershell
py -3 -m venv .venv
$python = '.\.venv\Scripts\python.exe'
& $python -X utf8 -m pip install -e '.[test]' 'setuptools>=77' wheel
& $python -X utf8 -m pytest -q
& $python -X utf8 scripts\build_package.py
& $python -X utf8 scripts\smoke_package.py
```

Эти команды проверяют функции проекта, отдельную установку ZIP и вызовы MCP. Отдельно выполните шаг 4 в Codex Desktop и Prism, чтобы проверить их интерфейсы и реальный импорт. Сверка не запускает Minecraft.
