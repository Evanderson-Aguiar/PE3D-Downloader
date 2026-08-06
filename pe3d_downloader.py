# -*- coding: utf-8 -*-
from qgis.PyQt.QtCore import QCoreApplication
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction
import os

from .pe3d_downloader_dialog import PE3DDownloaderDialog


class PE3DDownloaderPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.action = None
        self.dlg = None

    def tr(self, message):
        return QCoreApplication.translate("PE3DDownloader", message)

    def initGui(self):
        icon_path = os.path.join(self.plugin_dir, "icon.png")
        icon = QIcon(icon_path) if os.path.exists(icon_path) else QIcon()
        self.action = QAction(icon, self.tr("PE3D-Downloader"), self.iface.mainWindow())
        self.action.triggered.connect(self.run)

        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu(self.tr("&PE3D-Downloader"), self.action)

    def unload(self):
        if self.dlg is not None and self.dlg._task is not None:
            self.dlg._task.cancel()
        if self.action:
            self.iface.removeToolBarIcon(self.action)
            self.iface.removePluginMenu(self.tr("&PE3D-Downloader"), self.action)

    def run(self):
        if self.dlg is None:
            self.dlg = PE3DDownloaderDialog(self.iface)

        self.dlg.show()
        self.dlg.raise_()
        self.dlg.activateWindow()
