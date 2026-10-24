"""Service Windows 'ClaudeWatch' (pywin32). Mengurus lifecycle server.py.

Install/hapus lewat install.ps1 / uninstall.ps1 (butuh Administrator).
"""
import os
import sys
import threading
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import servicemanager  # noqa: E402
import win32event  # noqa: E402
import win32service  # noqa: E402
import win32serviceutil  # noqa: E402


def log(msg):
    try:
        with open(os.path.join(HERE, "service.log"), "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except OSError:
        pass


class ClaudeWatchService(win32serviceutil.ServiceFramework):
    _svc_name_ = "ClaudeWatch"
    _svc_display_name_ = "Claude Watch (monitor transkrip realtime)"
    _svc_description_ = "Web monitor sesi Claude Code di http://127.0.0.1:64000"

    def __init__(self, args):
        super().__init__(args)
        self.stop_evt = win32event.CreateEvent(None, 0, 0, None)
        self.srv = None

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        if self.srv:
            threading.Thread(target=self.srv.shutdown, daemon=True).start()
        win32event.SetEvent(self.stop_evt)

    def SvcDoRun(self):
        servicemanager.LogMsg(servicemanager.EVENTLOG_INFORMATION_TYPE, servicemanager.PYS_SERVICE_STARTED,
                              (self._svc_name_, ""))
        try:
            import server
            server.start_background()
            self.srv = server.make_server()
            log(f"listen {server.HOST}:{server.PORT} projects={server.PROJECTS}")
            t = threading.Thread(target=self.srv.serve_forever, daemon=True)
            t.start()
            win32event.WaitForSingleObject(self.stop_evt, win32event.INFINITE)
            self.srv.server_close()
        except Exception:
            log(traceback.format_exc())
            raise
        servicemanager.LogMsg(servicemanager.EVENTLOG_INFORMATION_TYPE, servicemanager.PYS_SERVICE_STOPPED,
                              (self._svc_name_, ""))


if __name__ == "__main__":
    if len(sys.argv) == 1:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(ClaudeWatchService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        win32serviceutil.HandleCommandLine(ClaudeWatchService)
