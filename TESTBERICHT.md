# Testbericht – Discord Trading Bot V2

Geprüft am 11.09.2026. Ergebnis: **50 automatisierte Tests bestanden**.

## Ausführung

```text
python3 -m unittest discover -s tests -q
Ran 50 tests in 0.400s
OK
```

Die Tests verwenden synthetische Kursdaten, temporäre Datenbanken und simulierte Brokerantworten. Sie prüfen insbesondere:

- Automatische Universumsauswahl, Liquiditätsfilter, RVOL-Rangfolge und den verbundenen Ablauf bis zur Bracket-Order und protokollierten Füllung.
- Unvollständige, zukünftige und veraltete Daten; fehlende Eröffnungsfenster und historische Vergleichsdaten.
- Stückzahlen, verfügbares Tagesbudget, gemeinsame Risikoreservierung und Korrelationssperre.
- Doppelte Orders, unklare Brokerantworten, Teilfüllungen, fehlende Stops, Schließungen und Wiederanlauf.
- Fortgesetzte Positionsüberwachung während einer langsamen oder fehlgeschlagenen Aktiensuche.
- Pagination, Tagescache, persistente Verlustsperren und Schutz vor unberechtigter Erstübernahme in Discord.

Zusätzlich erfolgreich geprüft:

- `python main.py --check` mit ausdrücklich fiktiven Zugangsdaten: Konfiguration und installierte Hauptabhängigkeiten; keine Anmeldung.
- Offline-Erzeugung des Discord-Clients, Registrierung aller 15 Slash-Befehle und sauberes Schließen.
- ZIP-Struktur und Integrität vor Bereitstellung.

## Aussagegrenzen

Es wurden keine echten Zugangsdaten verwendet, keine Discord-Nachrichten versendet und keine Orders an Alpaca übermittelt. Ein angemeldeter End-to-End-Test, ein Docker-Build und ein historischer Rendite-Backtest stehen aus. Die Tests belegen die geprüften Programmabläufe, keine Profitabilität oder garantierte Verlustobergrenze. Die laufende Installation wurde nicht geändert.

## Ergänzung für die Bereitstellung am 14.09.2026

Zwei zusätzliche Tests prüfen die vollständige SQLite-Sicherung vor dem Versionswechsel, den Erhalt von Verlustsperren und Orderjournal sowie den Schutz vor Überschreiben beim Wiederanlauf. V2 protokolliert außerdem den ersten erfolgreichen Paper-Brokerabgleich. Der Bericht vom 11.09. beschreibt die ursprüngliche ZIP; die Bereitstellung enthält diese Ergänzungen.

Erneute Prüfung am 14.09.2026: `python3 -m unittest discover -s tests -q` – **52 Tests bestanden** (0,897 s). Die lokale Startprüfung und die Offline-Registrierung der 15 Discord-Befehle waren ebenfalls erfolgreich.
