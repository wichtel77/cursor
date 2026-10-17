# -*- coding: utf-8 -*-

from Components.ActionMap import ActionMap
from Components.Label import Label
from Screens.VirtualKeyBoard import VirtualKeyBoard

from .. import _

from ..constants import DEFAULT_JELLYFIN_SERVER_INPUT, MAX_JELLYFIN_SERVER_PROFILES
from ..log import jd_log
from ..config import (
    jellydream_activate_server_profile,
    jellydream_normalize_server_profiles,
    jellydream_server_profile_is_logged_in,
    read_config
)
from ..http import jellyfin_login
from ..ui import JellyDreamBaseScreen


class JellyDreamLoginScreen(JellyDreamBaseScreen):
    SCREEN_NAME = "JellyDreamLoginScreen"
    AUTO_STATUS = True

    def __init__(self, session):
        JellyDreamBaseScreen.__init__(self, session)
        self.login_started = False

        try:
            jd_config = read_config() or {}
        except Exception:
            jd_config = {}

        self.server_profiles, self.profile_index = (
            jellydream_normalize_server_profiles(
                jd_config
            )
        )

        self.server = DEFAULT_JELLYFIN_SERVER_INPUT
        self.username = ""
        self.password = ""

        self.loadSelectedServerProfile()

        self["title"] = Label(_("Willkommen bei JellyDream"))
        self["hint"] = Label(_("Bitte melde dich mit deinen Jellyfin-Zugangsdaten an.\nDie Anmeldung wird nur auf dieser Box gespeichert."))
        self["info"] = Label("")
        self["status"].setText(_("Bitte warten..."))

        self["actions"] = ActionMap(
            [
                "OkCancelActions",
                "DirectionActions",
                "ColorActions"
            ],
            {
                "cancel": self.close,
                "ok": self.askServer,
                "left": self.previousServerProfile,
                "right": self.nextServerProfile,
                "yellow": self.activateSelectedServerProfile
            },
            -1
        )

        self.onLayoutFinish.append(self.updateInfo)

    def loadSelectedServerProfile(self):
        """
        Übernimmt Serveradresse und Benutzername des aktuell markierten
        Serverplatzes in die Eingabefelder.
        """
        try:
            if not (
                0 <= int(self.profile_index)
                < len(self.server_profiles)
            ):
                self.profile_index = 0

            profile = self.server_profiles[
                self.profile_index
            ]

            if not isinstance(profile, dict):
                profile = {}

            self.server = str(
                profile.get("server_url")
                or DEFAULT_JELLYFIN_SERVER_INPUT
            ).strip()

            self.username = str(
                profile.get("username")
                or ""
            ).strip()

            # Passwörter werden grundsätzlich nicht gespeichert und bei einem
            # Serverwechsel immer verworfen.
            self.password = ""

        except Exception as e:
            jd_log(
                "Server profile could not be loaded into the login screen: %s" % e,
                level="WARNING"
            )

            self.server = DEFAULT_JELLYFIN_SERVER_INPUT
            self.username = ""
            self.password = ""


    def previousServerProfile(self):
        if self.login_started:
            return

        self.profile_index = (
            self.profile_index - 1
        ) % MAX_JELLYFIN_SERVER_PROFILES

        self.loadSelectedServerProfile()
        self.updateInfo()


    def nextServerProfile(self):
        if self.login_started:
            return

        self.profile_index = (
            self.profile_index + 1
        ) % MAX_JELLYFIN_SERVER_PROFILES

        self.loadSelectedServerProfile()
        self.updateInfo()


    def activateSelectedServerProfile(self):
        """
        Aktiviert den markierten Server ohne erneute Passworteingabe.

        Das funktioniert nur, wenn für diesen Serverplatz bereits ein gültiger
        Token und eine Benutzer-ID gespeichert sind.
        """
        if self.login_started:
            return

        success, message = jellydream_activate_server_profile(
            self.profile_index
        )

        if success:
            self["status"].setText(_(message))
            self.close(True)
            return

        self.updateInfo()
        self["status"].setText(_(message))
    
    def updateInfo(self):
        try:
            profile = self.server_profiles[
                self.profile_index
            ]

            profile_is_logged_in = (
                jellydream_server_profile_is_logged_in(
                    profile
                )
            )

        except Exception:
            profile = {}
            profile_is_logged_in = False

        if not self.login_started:
            if profile_is_logged_in:
                self["status"].setText(_(
                    "LINKS/RECHTS = Serverplatz wechseln   "
                    "GELB = Gespeicherten Server aktivieren   "
                    "OK = Anmeldung erneuern"
                ))
            else:
                self["status"].setText(_(
                    "LINKS/RECHTS = Serverplatz wechseln   "
                    "OK = Server einrichten   EXIT = Abbrechen"
                ))
        else:
            self["status"].setText(_("Anmeldung wird eingerichtet ..."))

        server = self.server or "-"
        user = self.username or "-"
        pw_display = (
            "********"
            if self.password
            else "-"
        )

        profile_status = (
            _("Anmeldung gespeichert")
            if profile_is_logged_in
            else _("Leer / nicht angemeldet")
        )

        self["title"].setText(
            _("Jellyfin Server %s von %s")
            % (
                self.profile_index + 1,
                MAX_JELLYFIN_SERVER_PROFILES
            )
        )

        self["info"].setText(
            _("Serverplatz %s von %s\nStatus: %s\n\n1. Jellyfin Server\n%s\n\n2. Benutzername\n%s\n\n3. Passwort\n%s")
            % (
                self.profile_index + 1,
                MAX_JELLYFIN_SERVER_PROFILES,
                profile_status,
                server,
                user,
                pw_display
            )
        )
    
    def askServer(self):
        if self.login_started:
            return
        self.login_started = True
        self.updateInfo()        
        try:
            self.session.openWithCallback(
                self.serverDone,
                VirtualKeyBoard,
                title=_("Jellyfin Server Adresse"),
                text=(
                    self.server
                    or DEFAULT_JELLYFIN_SERVER_INPUT
                )
            )
        except Exception as e:
            self["status"].setText(_("Tastaturfehler: %s") % e)
            self.login_started = False
            self.updateInfo()            

    def serverDone(self, text):
        if text is None:
            self.login_started = False
            self.updateInfo()
            return

        server = str(text or "").strip()

        if server in (
            "",
            "http://",
            "https://"
        ):
            self.server = (
                server
                or DEFAULT_JELLYFIN_SERVER_INPUT
            )

            self.login_started = False
            self["status"].setText(_("Bitte eine IP-Adresse oder einen Servernamen eingeben."))
            self.updateInfo()
            return

        self.server = server.rstrip("/")
        self.updateInfo()

        try:
            self.session.openWithCallback(
                self.userDone,
                VirtualKeyBoard,
                title=_("Jellyfin Benutzer"),
                text=self.username or ""
            )
        except Exception as e:
            self["status"].setText(
                _("Tastaturfehler: %s") % e
            )
            self.login_started = False
            self.updateInfo()

    def userDone(self, text):
        if text is None:
            self.login_started = False
            self.updateInfo()
            return
        self.username = (text or "").strip()
        self.updateInfo()
        try:
            self.session.openWithCallback(
                self.passwordDone,
                VirtualKeyBoard,
                title=_("Jellyfin Passwort (wird verdeckt gespeichert)"),
                text=""
            )
        except Exception as e:
            self["status"].setText(_("Tastaturfehler: %s") % e)
            self.login_started = False
            self.updateInfo()

    def passwordDone(self, text):
        if text is None:
            self.login_started = False
            self.updateInfo()
            return
        self.password = text or ""
        self.updateInfo()
        self.attemptLogin()       

    def attemptLogin(self):
        if self._async_loading:
            return

        self["status"].setText(_("Anmeldung läuft..."))

        server = str(self.server or "").strip()
        username = str(self.username or "").strip()
        password = str(self.password or "")

        selected_profile_index = int(
            self.profile_index
        )

        self.runAsyncLoad(
            loader=lambda: jellyfin_login(
                server,
                username,
                password,
                profile_index=selected_profile_index
            ),
            finished_callback=self.loginFinished,
            message=(
                _("Anmeldung auf Serverplatz %s läuft ...")
                % (
                    selected_profile_index + 1
                )
            ),
            validate_auth_session=False
        )

    def loginFinished(self, result, error_message=""):
        self.login_started = False
        self.updateInfo()

        if error_message:
            self["status"].setText(
                _("Login fehlgeschlagen: %s") % error_message
            )
            return

        try:
            ok, message = result
        except Exception:
            ok, message = (
                False,
                _("Ungültige Login-Antwort")
            )

        if ok:
            self["status"].setText(
                _("Angemeldet als %s") % message
            )
            self.close(True)
        else:
            self["status"].setText(
                _("Login fehlgeschlagen: %s") % message
            )
