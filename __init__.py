# -*- coding: utf-8 -*-
def classFactory(iface):
    from .pe3d_downloader import PE3DDownloaderPlugin
    return PE3DDownloaderPlugin(iface)