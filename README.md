# Discord Link Resolver Bot

Discord-Bot zum Auflösen normaler HTTP/HTTPS-Redirects und Kurzlinks.

## Befehle

- `/resolve <url>` verfolgt normale serverseitige Weiterleitungen.
- `/linkinfo <url>` zeigt Basisinformationen zur URL.

Der Bot führt keinen Linkvertise-Bypass aus und umgeht keine Werbe-, Paywall- oder Schutzmechanismen.

## Railway

Start command:

```bash
python bot.py
```

Benötigte Umgebungsvariable:

```text
DISCORD_TOKEN
```
