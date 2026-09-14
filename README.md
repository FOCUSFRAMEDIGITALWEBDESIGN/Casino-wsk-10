# Discord Trading Bot V2

Vollständiges Python-Paket für **Alpaca-Papertrading** mit automatischer Aktienauswahl, ORB/VWAP-Strategie, Discord-Steuerung und dauerhaftem Orderjournal.

Stand: 14.09.2026. V2 mit automatischer Journalsicherung für den Wechsel von V1.

## Start

Python 3.11 oder neuer (empfohlen 3.12).

1. ZIP in einen eigenen Ordner entpacken.
2. Abhängigkeiten installieren: `python -m pip install -r requirements.txt`.
3. `.env.example` nach `.env` kopieren und die beiden Secrets ausfüllen.
4. `python main.py --check` prüft lokal die Konfiguration ohne Anmeldung.
5. `python main.py` startet den Bot.
6. Den Bot mit den Discord-Scopes `bot` und `applications.commands` zum eigenen Server einladen. Er benötigt Kanal ansehen, Nachrichten senden, Links einbetten und für CSV-Exporte Dateien anhängen. Ein privilegierter Message-Content-Intent ist nicht erforderlich.
7. Als Discord-Anwendungsbesitzer (oder berechtigtes Mitglied des Entwicklerteams) im gewünschten Textkanal `/start` verwenden. Der erste Brokerabgleich muss abgeschlossen sein. Mit `/scanner` den Fortschritt der automatischen Suche ansehen.

Nur zwei Secret-Variablen sind erforderlich:

```dotenv
DISCORD_TOKEN=dein_discord_bot_token
ALPACA_KEY=deine_paper_key_id:dein_paper_secret_key
```

`ALPACA_KEY` enthält also **zwei Alpaca-Werte**, getrennt durch einen Doppelpunkt. Es handelt sich um die Trading-API-Zugangsdaten des Paperkontos, nicht um einen Broker-API-Schlüssel. Zugangsdaten sind nicht in diesem Paket enthalten.

Beim ersten Start dieser Strategie sichert der Bot das vollständige SQLite-Journal als `paperbot-before-v2.sqlite3` im Datenverzeichnis. Die Sicherung wird nicht überschrieben. Danach bleibt die Automatik pausiert, bis `/start` erfolgt. Danach wird der Aktivierungszustand über Neustarts gespeichert. Ein anderer Nutzer kann den Bot nicht durch den ersten Befehl übernehmen. Die Berechtigung wird beim Start anhand der Discord-Anwendungsinformationen ermittelt.

## Was die automatische Suche tatsächlich macht

1. Lädt über Alpaca die aktiven handelbaren US-Aktien und ETFs auf NYSE, NASDAQ, AMEX, ARCA/NYSEARCA und BATS. OTC wird ausgelassen.
2. Prüft für das gelieferte Universum die vergangenen Tageskerzen. Daten werden in Gruppen abgerufen und vollständig paginiert.
3. Filtert: letzter Kurs mindestens 5 USD, ATR über 14 Tagesperioden mindestens 0,50 USD, mittlerer im **IEX-Feed** beobachteter Dollarumsatz mindestens 1 Mio. USD pro Tag.
4. Wählt standardmäßig 80 Werte mit dem höchsten historischen Dollarumsatz für die detaillierte Beobachtung. Manuell ergänzte Werte müssen dieselben Voraussetzungen erfüllen.
5. Lädt für diese Werte die Intraday-Historie und vergleicht das Volumen der ersten 15 Minuten mit dem Median der gleichen Eröffnungsfenster aus bis zu 20 früheren Sitzungen, mindestens 14.
6. Sortiert passende Ausbruchssignale nach RVOL. Die besten 20 RVOL-Werte zeigt `/scanner`; maximal drei Einstiegsversuche pro Tag können ausgeführt werden.

**Abdeckung:** IEX ist eine einzelne Börse. Das Paket liefert keinen vollständigen SIP-Marktüberblick. Die 80 Werte sind eine täglich automatisch gebildete Vorauswahl; nicht jede Aktie wird anschließend jede Minute ausführlich untersucht. Dadurch können spätere starke Bewegungen außerhalb dieser Auswahl fehlen. Fehlende Daten werden nicht erfunden. Der erste vollständige Datenaufbau kann mehrere Minuten benötigen; bereits veraltete Signale werden verworfen.

Der Tagesvergleich wird im Journal gespeichert. Intraday-Vortagsdaten werden während der Laufzeit zwischengespeichert; der aktuelle Handelstag wird bei Folgeabfragen neu geladen, damit Korrekturen berücksichtigt werden. Nach einem Neustart wird die Intraday-Historie neu aufgebaut. Ein Datenfehler kann die Aktiensuche stoppen, ohne ihren langen Datenabruf unter den Positionslock zu nehmen.

## Einstieg und Ausstieg

- **Long:** Nach der 15-Minuten-Eröffnungsspanne schließt eine vollständige 5-Minuten-Kerze erstmals über dem Eröffnungshoch und über dem Tages-VWAP. Der vorherige Kerzenschluss lag noch nicht darüber.
- **Short:** Umgekehrt unter Eröffnungstief und VWAP; Alpaca muss Shortbarkeit, einfache Leihbarkeit und Marginfähigkeit bestätigen.
- Relatives Eröffnungsvolumen mindestens 1,5.
- Einstiege frühestens nach der ersten vollständigen Bestätigungskerze, bis 90 Minuten nach Sitzungsbeginn und spätestens 30 Minuten vor Börsenschluss.
- Maximal 90 Sekunden seit dem Ende der Signalkerze und zehn Sekunden Quote-Alter. Zukunftsquotes sind ungültig. Aktuelle Sitzungskerzen müssen lückenlos sein und plausible OHLCV-/VWAP-Werte enthalten.
- Spread maximal 0,10 % des Mittelkurses sowie maximal 10 % des Stopabstands. Kursabweichung vom Signal zusätzlich maximal 0,5 Intraday-ATR.
- Stopabstand: Maximum aus 1,5 × Intraday-ATR und halber Eröffnungsspanne. ATR ist hier der einfache Mittelwert von 14 vollständigen 5-Minuten-True-Ranges einschließlich Übergängen zwischen Sitzungen. Zulässiger Abstand 0,15–2 % des Einstiegs.
- Einstieg per Limit-Bracket mit Stop-Market und Ziel bei 2R. Das Limit erlaubt höchstens den konfigurierten Aufschlag/Abschlag zum aktuellen Ask/Bid; es garantiert keine Füllung.
- Ungefüllte Einstiege nach 120 Sekunden abbrechen. Teilfüllungen kontrolliert schließen, weil Bracket-Ausstiege erst nach vollständiger Einstiegsfüllung aktiv werden.
- Zehn Minuten vor dem kalendarischen Börsenschluss Positionen schließen lassen. Verkürzte Sitzungen kommen aus dem Brokerkalender. Außerhalb der regulären Sitzung werden keine neuen Orders für diese Strategie ausgelöst; nicht erledigte Schließungen werden weiter vorgemerkt.

Alle Parameter sind Forschungsentscheidungen. Eine höhere Rentabilität gegenüber V1 wurde nicht nachgewiesen. Der neue aktive Handelsweg nutzt ORB; die EMA-Hilfsfunktion bleibt lediglich für Regressionstests im Quellcode.

## Risikogrenzen

| Grenze | Standard |
| --- | --- |
| Geplantes Risiko einschließlich Kostenpuffer je Einstieg | maximal 0,25 % des aktuellen Kontowerts |
| Reserviertes gemeinsames Positions-/Orderrisiko | maximal 0,75 % |
| Kapitaleinsatz je Position / gesamt | maximal 20 % / 60 % |
| Positionen / tägliche Einstiegsversuche | maximal 3 / 3 |
| Tägliche Verlustsperre | 3 % gegenüber gespeichertem vorherigem Schlusskontowert |
| Modellierter Kostenpuffer zur Stückzahlberechnung | 0,05 % des Einstiegspreises pro Aktie |

Das verbleibende Tagesbudget wird vor neuen Orders berücksichtigt. Offene und noch ausstehende Einstiege reservieren Risiko. Bei stark gleichgerichteten Expositionen (vorzeichenbereinigte Renditekorrelation über 0,85 auf mindestens zehn gemeinsamen historischen Tagesrenditen) wird ein weiterer Einstieg abgelehnt. Fehlt die Vergleichshistorie, wird ebenfalls kein zusätzliches Risiko eröffnet. Dieser kurze Korrelationsvergleich ist eine konservative Konzentrationsprüfung, keine Diversifikationsgarantie.

Tagessperren und Order-IDs überleben Neustarts. Nach unklarer Orderantwort wird zuerst abgeglichen, nicht blind erneut gesendet. Ein gefüllter Einstieg ohne erkennbaren aktiven Brokerstop wird zur Schließung vorgemerkt. Stops, Limits und Tagessperren können wegen Kurslücken, Halts und Verbindungsproblemen keine exakte Verlustobergrenze garantieren.

Die Positionsüberwachung, die Aktiensuche und die Discord-Zustellung laufen als getrennte asynchrone Aufgaben. Lange Universumsabfragen halten den Orderlock nicht. Brokerabfragen selbst können bei Störungen weiterhin verzögert sein. Brokerseitige Stops bleiben daher wesentlich.

## Befehle

| Befehl | Zweck |
| --- | --- |
| `/start` | Meldekanal setzen und Automatik aktivieren |
| `/scanner` | Auswahlfortschritt und RVOL-Rangliste |
| `/status`, `/konto`, `/positionen` | Markt-/Brokerstatus und Kontowerte |
| `/pause` | Neue und ungefüllte Einstiege stoppen; gefüllte Positionen weiter überwachen |
| `/schliessen symbol:XYZ` | Einzelposition kontrolliert schließen |
| `/notstopp bestaetigen:True` | Automatik pausieren und alle Positionen/Orders des dedizierten Paperkontos schließen lassen |
| `/watchlist`, `/watch_add`, `/watch_remove` | Optionale manuelle Ergänzungen; Auswahländerungen werden beim nächsten Tagesaufbau berücksichtigt |
| `/verlauf`, `/export` | Journal und CSV-Daten |
| `/auftrag_pruefen` | Unklare Order anhand ihrer Client-ID abgleichen |

Regelmäßiger Status standardmäßig alle 30 Minuten bei offener Börse und alle drei Stunden außerhalb. Zusätzlich Meldungen über ausgewählte Signale, Orderübermittlung, bestätigte Füllungen, Schließungen und Fehler. Keine Mindestanzahl an Trades; null Trades ist ein gültiges Ergebnis.

## Railway / Sparked Host

Enthalten sind `Dockerfile`, `railway.toml`, `requirements.txt` und `main.py`.

- Railway: ein dauerhaftes Volume auf `/data`, `DATA_DIR=/data`, genau eine Instanz, kein Sleep-Modus. Zwei Secrets wie oben eintragen. Startbefehl `python -u main.py`.
- Sparked Host: Python 3.11+ wählen, Abhängigkeiten installieren, dieselben Secrets setzen und `python -u main.py` starten. Das `data`-Verzeichnis muss erhalten bleiben.
- Dies ist ein Hintergrundprozess, kein Webserver; es ist kein HTTP-Healthcheck vorgesehen.

**Upgrade:** Vor einem späteren Austausch V1 pausieren und offene Orders/Positionen klären. Den alten Prozess beenden und das komplette Datenverzeichnis sichern. V2 muss bei Weiterverwendung desselben Paperkontos dasselbe Journal benutzen; die Datenbank nicht löschen. Niemals V1 und V2 gleichzeitig auf demselben Konto betreiben. Bei erstmaliger V2-Nutzung ist `/start` erneut nötig. Es gibt keine automatische Rückmigration oder Bereitstellung in diesem Paket.

Ein eigenständiges neues Paperkonto kann alternativ mit einem neuen leeren Datenverzeichnis genutzt werden. Ein unbekanntes bereits benutztes Konto wird nicht stillschweigend als leeres Konto übernommen.

## Tests und bekannte Grenzen

`python -m unittest discover -s tests -v`

Der beiliegende Testbericht dokumentiert 52 bestandene automatisierte Tests: darunter ein Ablauf von Universumsdaten bis Order/Füllungsjournal, Zeitprüfung, Risikobudgets, Konzentrationsprüfung, fehlende Stops, Seitenwechsel, Wiederanlauf und parallele Überwachung.

Die Tests benutzen synthetische Daten und simulierte Alpaca-/Discord-Antworten. **Kein angemeldeter Alpaca-/Discord-End-to-End-Test und kein Rendite-Backtest** wurden ausgeführt. Die lokale Startprüfung und die Registrierung der Discord-Befehle wurden ohne Anmeldung geprüft. Docker wurde hier nicht gebaut. Bei der ersten echten Paper-Nutzung sind insbesondere Datenberechtigung, IEX-Abdeckung und Brokerantworten zu beobachten.

Nicht enthalten: Nachrichtendienst, Social-Media-Suche, KI-Nachtraining, Echtgeldausführung, Premarket-/Afterhours-Handelsstrategie und vollständiger historischer Replay-Backtester. Alpaca-Paperwerte berücksichtigen nicht alle realen Handelskosten; der Stückzahlpuffer ist kein Kostenabzug aus Alpacas ausgewiesener Rendite.

## Technische Quellen

- [Alpaca Assetliste](https://docs.alpaca.markets/us/reference/get-v2-assets-1)
- [Historische Kerzen, Feed und Pagination](https://docs.alpaca.markets/us/reference/stockbars)
- [IEX und SIP](https://docs.alpaca.markets/us/docs/market-data-faq)
- [Order- und Bracket-Verhalten](https://docs.alpaca.markets/us/docs/orders-at-alpaca)
- [Grenzen von Papertrading](https://docs.alpaca.markets/us/docs/paper-trading)
- [ORB-Forschung: Ursprung der Hypothese, keine Renditeprognose für diesen Code](https://concretumgroup.com/wp-content/uploads/2026/02/A-Profitable-Day-Trading-Strategy-For-The-U.S.-Equity-Market.pdf)

Quellen abgerufen am 11.09.2026. Einzelne Schwellen und die konkrete Signalkombination sind eigene, bislang nicht empirisch optimierte Entwurfsentscheidungen.
