# Client configuration examples / Примеры подключения

The server speaks MCP over stdio: it is launched as a subprocess and exchanges
newline-delimited JSON messages. Any MCP client can run it.

Сервер работает по MCP через стандартный ввод-вывод: его запускают как обычную
программу и обмениваются с ней строками JSON. Подойдёт любой клиент MCP.

## JSON-style clients (Claude Desktop and similar)

```json
{
  "mcpServers": {
    "yandex-calendar": {
      "command": "python3",
      "args": ["/path/to/yandex-calendar-mcp/src/server.py"],
      "env": {
        "YANDEX_USERNAME": "name@yandex.ru",
        "YANDEX_PASSWORD": "app-password",
        "YANDEX_TIMEZONE": "Europe/Moscow"
      }
    }
  }
}
```

## YAML-style clients

```yaml
mcp_servers:
  yandex-calendar:
    command: python3
    args:
      - /path/to/yandex-calendar-mcp/src/server.py
    env:
      YANDEX_USERNAME: ${YANDEX_USERNAME}
      YANDEX_PASSWORD: ${YANDEX_PASSWORD}
      YANDEX_TIMEZONE: Europe/Moscow
      YANDEX_TRASH_DIR: /var/lib/yandex-calendar/trash
    connect_timeout: 60.0
    enabled: true
```

## Keep the password out of the config

Where the client supports it, reference a variable (`${YANDEX_PASSWORD}`)
instead of writing the value. Store the value in the client's own secrets file
with owner-only permissions. Never commit it.

Если клиент это умеет, пишите в конфиге ссылку `${YANDEX_PASSWORD}`, а не сам
пароль. Значение держите в отдельном файле с правами «только владельцу»
и никогда не кладите в репозиторий.

## Dependencies in a restricted environment

If the runtime has no `pip`, install the libraries into a directory and point
`PYTHONPATH` at it:

```
uv pip install --target /path/to/lib -r requirements.txt
```

```yaml
    env:
      PYTHONPATH: /path/to/lib
```
