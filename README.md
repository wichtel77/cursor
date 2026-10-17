# JellyDream Modern

Jellyfin-Client für Enigma2. Filme, Serien, Sammlungen und Musik direkt auf der Box, mit Oberflächen für HD, Full HD und Ultra HD.

## Installation

1. Das aktuelle IPK von den [Releases](https://github.com/wichtel77/cursor/releases) laden.
2. Auf der Box installieren, zum Beispiel über die Softwareverwaltung oder mit:

   ```sh
   opkg install enigma2-plugin-extensions-jellydreammodern_<version>_all.ipk
   ```

3. Die Oberfläche neu starten.

Das Plugin erscheint im Plugin-Menü und im Erweiterungsmenü. Der Paketname ist `enigma2-plugin-extensions-jellydreammodern`.

## Start

Beim ersten Start Server, Benutzer und Passwort eintragen. Es können mehrere Serverprofile gespeichert und später gewechselt werden. Die Anmeldung bleibt in `/etc/enigma2/jellydreammodern.json` erhalten.

## Bibliothek

| Bereich | Inhalt |
| --- | --- |
| Filme | Filmliste mit Genre, Sortierung und Fortschritt am Cover |
| Serien | Serien, Staffeln und Folgen, inklusive Weiterschauen |
| Sammlungen | Jellyfin-Sammlungen, sofern welche vorhanden sind |
| Musik | Alben und Titel |
| Favoriten | Filme, Serien, Folgen und Sammlungen, die als Favorit markiert sind |
| Suche | Suche in der Bibliothek |

Auf der Startseite liegen die neuesten Titel und die zuletzt angesehenen Einträge. Filme und Serien können als gesehen oder ungesehen markiert werden. Bei Musik heißt derselbe Status gehört oder ungehört.

## Bedienung

| Taste | Funktion |
| --- | --- |
| OK | Auswählen oder abspielen |
| Grün | Ansicht und Sortierung, in den Einstellungen Speichern |
| Gelb | Wiedergabestatus oder Favorit |
| Blau | Bibliothek aktualisieren |
| EXIT / BACK | Zurück oder Abbrechen |

Die farbigen Tasten werden in der Statuszeile als farbige Kästen gezeigt, die Richtungstasten als Pfeile.

## Sprachen

Die Oberfläche folgt der Box-Sprache, wenn eine Übersetzung vorhanden ist. Sonst wird Englisch verwendet.

Deutsch, Englisch, Französisch, Italienisch, Spanisch, Polnisch, Niederländisch, Türkisch, Arabisch und Russisch.

## Hinweise

Nach der Installation und nach einem Update die Oberfläche neu starten. Beim Entfernen des Plugins wird das Programmverzeichnis gelöscht. Eine Aktualisierung lässt die gerade installierten Dateien stehen.
