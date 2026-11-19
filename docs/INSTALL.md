# Установка Modpack Builder

Проверяемый путь — локальная установка на Linux. Нужны Python 3.11 или новее с модулем `venv`, Bash, доступ в интернет для установки Python-зависимостей и Codex с поддержкой локальных плагинов. Для работы с каталогом нужен Chrome или Chromium. Prism Launcher нужен для импорта готового ZIP.

## 1. Получите файлы

Войдите в GitHub под аккаунтом, у которого есть доступ к репозиторию. Скачайте файл `minecraft-mod-curator-0.7.0-plugin.zip` со страницы [выпуска 0.7.0](https://github.com/IKirillka/modpack-builder/releases/tag/v0.7.0), распакуйте его и откройте терминал в папке `minecraft-mod-curator`. Для сборки из исходников используйте Git.

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

Если в вашей версии Codex есть локальный MCP, но отсутствует команда `codex plugin`, вместо регистрации плагина подключите сервер напрямую и передайте инструкции из `skills/curate-minecraft-mods/SKILL.md` в проект, где будете им пользоваться.

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

Ключ CurseForge API необязателен для работы через браузер. Если нужен официальный API, передайте `CURSEFORGE_API_KEY` в окружение локального сервера. Храните ключ вне репозитория.

ZIP и локальная конфигурация рассчитаны на клиент с доступом к вашему компьютеру. Для ChatGPT Web нужен отдельно настроенный путь подключения удалённого MCP. Такой сценарий для этого пакета не проверен. [Документация OpenAI по локальным и удалённым MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).
