# Установка Modpack Builder

На Windows следуйте [инструкции для PowerShell](WINDOWS.md). Ниже приведена установка на Linux. Нужны Python 3.11 или новее с модулем `venv`, Bash, доступ в интернет для установки Python-зависимостей и Codex с поддержкой локальных плагинов. Для работы с каталогом нужен Chrome или Chromium. Prism Launcher нужен для импорта готового ZIP.

## 1. Получите файлы

Возьмите собранный `minecraft-mod-curator-<версия>-plugin.zip` или скачайте его со [страницы выпусков](https://github.com/IKirillka/modpack-builder/releases). Распакуйте его и откройте терминал в папке `minecraft-mod-curator`. Для установки из исходников клонируйте репозиторий.

```bash
git clone https://github.com/IKirillka/modpack-builder.git
cd modpack-builder
```

## 2. Установите локальный компонент

В этой же папке выполните команды.

```bash
bash scripts/install.sh
python3 scripts/launch.py init --destination "$HOME/Minecraft/ModCuratorPacks" --alias packs --transport cdp
python3 scripts/start_browser.py
```

Оставьте открытым отдельное окно Chrome или Chromium, которое запустит скрипт. Поддерживается также Chrome из Flatpak. Соединение с браузером идёт через `127.0.0.1:9222`. Если сайт покажет проверку, пройдите её в открытой вкладке и попросите Codex продолжить задание по ID.

Папка `$HOME/Minecraft/ModCuratorPacks` предназначена для экспортируемых ZIP. Задания, корзина, кэш и настройки хранятся в `~/.local/share/mod-curator`. Если компонент уже настроен и нужная папка зарегистрирована, повторно выполнять `init` не требуется.

## 3. Подключите Codex

Всё ещё из папки с файлами запустите команды.

```bash
codex plugin marketplace add "$PWD"
codex plugin add minecraft-mod-curator@minecraft-mod-tools-local
```

Откройте новый локальный чат Codex и проверьте, что **Modpack Builder** доступен. Команда `codex plugin marketplace list` показывает зарегистрированные каталоги. При обновлении существующей установки используйте её каталог и переустановите плагин по тому же ID. [Документация OpenAI по каталогам плагинов](https://developers.openai.com/plugins/build/plugins).

Если в вашей версии Codex есть локальный MCP, но отсутствует команда `codex plugin`, подключите сервер напрямую. В новом чате попросите Codex прочитать `skills/curate-minecraft-mods/SKILL.md` из папки плагина.

```bash
codex mcp add minecraft_mod_curator -- python3 "$PWD/scripts/launch.py" serve --transport stdio
codex mcp list
```

Для одной установки достаточно одного подключения. После обновления отдельно запущенного MCP перезапустите его. [Документация OpenAI по MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).

## 4. Попробуйте в чате

> Используй Modpack Builder. Подбери сборку для Minecraft 1.21.1 и NeoForge. Основа — автоматизация на Create, без декоративных дублей. Для ZIP используй папку packs.

Когда список готов, попросите проверить сборку. После проверки явно запросите экспорт проверенной ревизии. В Prism Launcher выберите **Добавить экземпляр → Импорт**, укажите ZIP и дождитесь установки. Если Prism попросит какой-либо файл вручную, используйте указанный им официальный источник.

Для просмотра политики хранения скажите «Покажи настройки автоочистки». [Корзина и автоочистка](JOB_CLEANUP.md).

## Проверка локальной установки

```bash
python3 scripts/launch.py --help
python3 scripts/launch.py cleanup settings
```

## Ключ CurseForge API

В версии 0.8.2 и новее для настройки откройте [раздел API Keys](https://console.curseforge.com/#/api-keys) и скопируйте ключ, которому разрешено чтение метаданных Minecraft. В папке установленного или распакованного плагина выполните:

```bash
python3 scripts/launch.py curseforge-key set
python3 scripts/launch.py curseforge-key status
```

На Windows используйте `py -3 .\scripts\launch.py curseforge-key set`. Вставьте ключ в скрытое поле в терминале. Команда проверит доступ к проекту Minecraft и его файлам, затем сохранит ключ в системном хранилище. Через MCP видны только наличие и источник настройки. Для работы с новыми заданиями достаточно перезапустить установленный MCP, если он ещё запущен на старой версии.

`curseforge-key check` повторно проверяет доступ, а `curseforge-key unset` удаляет локальную запись из системного хранилища. Значение `CURSEFORGE_API_KEY` в окружении MCP имеет приоритет. Оно подходит для сервера без доступного хранилища учётных данных и удаляется отдельно из окружения. Ошибка 401 или 403 на этапе проверки может означать, что ключ не имеет доступа к Minecraft. [Документация CurseForge REST API](https://docs.curseforge.com/rest-api/#authentication)

Официальный API используется для описаний, файлов и объявленных связей найденных проектов. Сам обход каталога CurseForge с фильтрами и сортировкой продолжает читать реальные страницы сайта. Доступ через браузер может потребоваться даже с настроенным ключом.

ZIP и локальная конфигурация рассчитаны на клиент с доступом к вашему компьютеру. Для ChatGPT Web нужен отдельно настроенный путь подключения удалённого MCP. Такой сценарий для этого пакета не проверен. [Документация OpenAI по локальным и удалённым MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).
