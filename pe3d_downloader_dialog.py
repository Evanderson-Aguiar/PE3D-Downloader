# -*- coding: utf-8 -*-
import os
import tempfile
from datetime import datetime

from qgis.PyQt import uic
from qgis.PyQt.QtCore import QSettings, Qt, pyqtSignal
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QDialog, QFileDialog, QMessageBox

from qgis.core import (
    QgsProject,
    QgsMapLayerType,
    QgsRasterLayer,
    QgsRectangle,
    QgsVectorLayer,
    QgsWkbTypes,
    QgsGeometry,
    QgsPointXY,
    QgsApplication,
    QgsTask,
)

from qgis.gui import QgsMapTool, QgsRubberBand

from .grid_selector import GridSelector
from .downloader import PE3DDownloader, Product, CancelledError


FORM_CLASS, _ = uic.loadUiType(
    os.path.join(os.path.dirname(__file__), "pe3d_downloader_dialog_base.ui")
)


class PE3DDownloadTask(QgsTask):
    """Executa rede, extração e GDAL fora da thread da interface."""

    log_message = pyqtSignal(str)
    product_progress = pyqtSignal(int)
    status_message = pyqtSignal(str)

    def __init__(
        self,
        tiles,
        products,
        out_dir,
        aoi_bounds,
        aoi_srs_wkt,
        on_finished,
        completed_downloads=None,
        partial_only=False,
        run_suffix=None,
    ):
        super().__init__("Baixar e processar dados PE3D", QgsTask.CanCancel)
        self.tiles = tiles
        self.products = products
        self.out_dir = out_dir
        self.aoi_bounds = aoi_bounds
        self.aoi_srs_wkt = aoi_srs_wkt
        self.on_finished_callback = on_finished
        self.completed_downloads = completed_downloads or {product: [] for product in products}
        self.partial_only = partial_only
        self.run_suffix = run_suffix or datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        self.downloader = None
        self.created_vrts = []
        self.error_message = None
        self.was_cancelled = False

    def _log(self, message):
        self.log_message.emit(str(message))

    def cancel(self):
        if self.downloader is not None:
            self.downloader.cancel()
        super().cancel()

    def run(self):
        downloaded_tifs = {product: [] for product in self.products}
        product_count = max(len(self.products), 1)

        def update_progress(product_index, product_percent, status):
            product_percent = max(0, min(100, int(product_percent)))
            self.product_progress.emit(product_percent)
            self.status_message.emit(status)
            self.setProgress((product_index + product_percent / 100) / product_count * 100)

        self.downloader = PE3DDownloader(self._log)
        try:
            for product_index, product in enumerate(self.products):
                downloads = self.completed_downloads.setdefault(product, [])
                if not self.partial_only:
                    self._log(f"Iniciando produto {product.value}: fase de download.")
                    tile_count = max(len(self.tiles), 1)
                    for tile_index, tile in enumerate(self.tiles):
                        if self.isCanceled():
                            raise CancelledError("Operação cancelada pelo usuário.")
                        if product == Product.MDE:
                            url = tile["mde"]
                        elif product == Product.MDT:
                            url = tile["mdt"]
                        else:
                            url = tile["orto"]

                        def download_progress(percent, index=tile_index):
                            phase = ((index + percent / 100) / tile_count) * 65
                            update_progress(
                                product_index,
                                phase,
                                f"Baixando {product.value} — {index + 1}/{tile_count}",
                            )

                        self.downloader.progress = download_progress
                        update_progress(
                            product_index,
                            tile_index / tile_count * 65,
                            f"Baixando {product.value} — {tile_index + 1}/{tile_count}",
                        )
                        self._log(
                            f"[{tile_index + 1}/{tile_count}] Baixando {product.value} | "
                            f"fid={tile['fid']}"
                        )
                        download = self.downloader.download_by_url(
                            product, tile["fid"], url, self.out_dir
                        )
                        downloads.append(download)

                if not downloads:
                    continue
                self._log(f"Produto {product.value}: fase de extração.")
                download_count = len(downloads)
                extraction_start = 0 if self.partial_only else 65
                extraction_span = 80 if self.partial_only else 25
                for download_index, download in enumerate(downloads):
                    if self.isCanceled():
                        raise CancelledError("Operação cancelada pelo usuário.")

                    def extraction_progress(percent, index=download_index):
                        phase = extraction_start + (
                            (index + percent / 100) / download_count * extraction_span
                        )
                        update_progress(
                            product_index,
                            phase,
                            f"Extraindo {product.value} — {index + 1}/{download_count}",
                        )

                    self.downloader.progress = extraction_progress
                    paths = self.downloader.extract_downloaded(download)
                    downloaded_tifs[product].extend(paths)

                self._log(f"Produto {product.value}: gerando e recortando VRT.")
                vrt_start = 80 if self.partial_only else 90

                def vrt_progress(percent):
                    update_progress(
                        product_index,
                        vrt_start + percent / 100 * (100 - vrt_start),
                        f"Gerando VRT de {product.value}",
                    )

                self.downloader.progress = vrt_progress
                output_name = f"PE3D_{product.value}_{self.run_suffix}"
                vrt_path = os.path.join(self.out_dir, output_name + ".vrt")
                self.downloader.build_clipped_vrt(
                    vrt_path, downloaded_tifs[product], self.aoi_bounds, self.aoi_srs_wkt
                )
                self.created_vrts.append((vrt_path, output_name))
                self._log(f"VRT recortado criado: {vrt_path}")
                update_progress(product_index, 100, f"{product.value} concluído")
            self.setProgress(100)
            return True
        except CancelledError:
            self.was_cancelled = True
            return False
        except Exception as ex:
            self.error_message = str(ex)
            self._log(f"ERRO: {ex}")
            return False
        finally:
            if self.downloader is not None:
                self.downloader.close()
                self.downloader = None

    def finished(self, result):
        self.on_finished_callback(self, result)


class _ExtentTool(QgsMapTool):
    """
    Desenha retângulo com preview (rubber band) enquanto arrasta.
    Ao soltar, retorna QgsRectangle no CRS do mapa.
    """
    def __init__(self, canvas, on_done, log_fn=None):
        super().__init__(canvas)
        self.canvas = canvas
        self.on_done = on_done
        self.log = log_fn or (lambda _: None)

        self._start_pt = None

        self._rb = QgsRubberBand(self.canvas, QgsWkbTypes.PolygonGeometry)
        self._rb.setStrokeColor(Qt.red)
        self._rb.setFillColor(Qt.transparent)
        self._rb.setWidth(2)
        self._rb.hide()

    def _set_rb_from_rect(self, rect: QgsRectangle):
        pts = [
            QgsPointXY(rect.xMinimum(), rect.yMinimum()),
            QgsPointXY(rect.xMaximum(), rect.yMinimum()),
            QgsPointXY(rect.xMaximum(), rect.yMaximum()),
            QgsPointXY(rect.xMinimum(), rect.yMaximum()),
            QgsPointXY(rect.xMinimum(), rect.yMinimum()),
        ]
        geom = QgsGeometry.fromPolygonXY([pts])
        self._rb.setToGeometry(geom, None)
        self._rb.show()

    def canvasPressEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        self._start_pt = e.mapPoint()
        self._rb.reset(QgsWkbTypes.PolygonGeometry)
        self._rb.show()

    def canvasMoveEvent(self, e):
        if self._start_pt is None:
            return
        p = e.mapPoint()
        rect = QgsRectangle(
            min(self._start_pt.x(), p.x()),
            min(self._start_pt.y(), p.y()),
            max(self._start_pt.x(), p.x()),
            max(self._start_pt.y(), p.y()),
        )
        if rect.width() > 0 and rect.height() > 0:
            self._set_rb_from_rect(rect)

    def canvasReleaseEvent(self, e):
        if e.button() != Qt.LeftButton or self._start_pt is None:
            return

        end_pt = e.mapPoint()
        rect = QgsRectangle(
            min(self._start_pt.x(), end_pt.x()),
            min(self._start_pt.y(), end_pt.y()),
            max(self._start_pt.x(), end_pt.x()),
            max(self._start_pt.y(), end_pt.y()),
        )

        self._start_pt = None

        if rect.width() <= 0 or rect.height() <= 0:
            self._rb.hide()
            return

        self.on_done(rect)

    def clear(self):
        try:
            self._rb.hide()
            self._rb.reset(QgsWkbTypes.PolygonGeometry)
        except RuntimeError as ex:
            self.log(f"Não foi possível limpar a marcação da AOI: {ex}")

    def deactivate(self):
        self._start_pt = None
        self.clear()
        super().deactivate()


class PE3DDownloaderDialog(QDialog, FORM_CLASS):
    AOI_CANVAS = "Extensão da tela"
    AOI_LAYER = "Extensão de camada"
    AOI_DRAW = "Desenhar retângulo"

    URL_FIELD_MDE = "MDE"
    URL_FIELD_MDT = "MDT"
    URL_FIELD_ORTO = "ORTOFOTO"

    EMBEDDED_GPKG_REL = os.path.join("data", "pe3d_grid.gpkg")
    EMBEDDED_LAYER_NAME = "grid"

    def __init__(self, iface):
        super().__init__(iface.mainWindow())
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.setupUi(self)

        # ✅ ÍCONE NO TÍTULO DA JANELA
        self._apply_window_icon()

        self._settings = QSettings()
        self._aoi_rect_map_crs = None
        self._aoi_draw_crs = None
        self._prev_map_tool = None

        self._embedded_grid = None
        self._draw_tool = None

        self._downloader = None
        self._task = None
        self._running = False

        self._init_ui()
        self._wire_events()

    def _apply_window_icon(self):
        """
        Define o ícone do diálogo usando icon.png do plugin.
        """
        plugin_dir = os.path.dirname(__file__)
        icon_path = os.path.join(plugin_dir, "icon.png")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

    def _init_ui(self):
        self.cmbAoiMode.clear()
        self.cmbAoiMode.addItems([self.AOI_CANVAS, self.AOI_LAYER, self.AOI_DRAW])

        default_out = self._settings.value("PE3DDownloader/out_dir", tempfile.gettempdir(), type=str)
        self.txtOutDir.setText(default_out)

        self.chkMDE.setChecked(True)
        self.chkMDT.setChecked(True)
        self.chkORTO.setChecked(False)

        self._refresh_layers()

        self.progressBar.setValue(0)
        self.progressProduct.setValue(0)
        self.lblProductStatus.setText("Aguardando...")
        self.txtLog.setPlainText("")
        self._update_aoi_info()

        self.btnCancel.setEnabled(False)

        try:
            self._embedded_grid = self._load_embedded_grid_layer()
            self._log("Grade PE3D embutida carregada (sem adicionar ao projeto).")
        except Exception as ex:
            self._embedded_grid = None
            self._log(f"ERRO ao carregar grade embutida: {ex}")

    def _wire_events(self):
        self.btnClose.clicked.connect(self.close)
        self.btnBrowse.clicked.connect(self._browse_out_dir)
        self.btnRun.clicked.connect(self._run)
        self.btnCancel.clicked.connect(self._cancel)

        self.cmbAoiMode.currentIndexChanged.connect(self._update_aoi_info)
        self.cmbLayer.currentIndexChanged.connect(self._update_aoi_info)
        self.btnPickRect.clicked.connect(self._activate_draw_tool)

    def showEvent(self, e):
        super().showEvent(e)
        self._refresh_layers()
        self._update_aoi_info()

        if self._embedded_grid is None or not self._embedded_grid.isValid():
            try:
                self._embedded_grid = self._load_embedded_grid_layer()
                self._log("Grade PE3D embutida carregada (tentativa ao abrir).")
            except Exception as ex:
                self._embedded_grid = None
                self._log(f"ERRO ao carregar grade embutida (ao abrir): {ex}")

    def closeEvent(self, e):
        if self._running:
            e.ignore()
            self._log("Cancele a operação antes de fechar a janela.")
            return
        if self._draw_tool is not None:
            self._draw_tool.clear()
        super().closeEvent(e)

    def _log(self, msg: str):
        self.txtLog.appendPlainText(str(msg))

    def _browse_out_dir(self):
        path = QFileDialog.getExistingDirectory(self, "Escolha a pasta de saída", self.txtOutDir.text())
        if path:
            self.txtOutDir.setText(path)

    def _refresh_layers(self):
        self.cmbLayer.clear()
        for lyr in QgsProject.instance().mapLayers().values():
            if lyr.type() in (QgsMapLayerType.VectorLayer, QgsMapLayerType.RasterLayer):
                self.cmbLayer.addItem(lyr.name(), lyr.id())

    def _get_layer_by_combo(self, combo):
        layer_id = combo.currentData()
        if not layer_id:
            return None
        return QgsProject.instance().mapLayer(layer_id)

    def _load_embedded_grid_layer(self) -> QgsVectorLayer:
        plugin_dir = os.path.dirname(__file__)
        gpkg_path = os.path.join(plugin_dir, self.EMBEDDED_GPKG_REL)

        if not os.path.exists(gpkg_path):
            raise RuntimeError(
                "Arquivo da grade embutida não encontrado.\n"
                f"Esperado em: {gpkg_path}"
            )

        uri = f"{gpkg_path}|layername={self.EMBEDDED_LAYER_NAME}"
        lyr = QgsVectorLayer(uri, "PE3D Grid (embutida)", "ogr")
        if not lyr.isValid():
            raise RuntimeError(
                "Não foi possível carregar a grade embutida.\n"
                f"Verifique o layername '{self.EMBEDDED_LAYER_NAME}' dentro do gpkg."
            )
        return lyr

    def _validate_url_fields_exist(self, grid_lyr, products):
        names = {f.name() for f in grid_lyr.fields()}
        required = []
        if Product.MDE in products:
            required.append(self.URL_FIELD_MDE)
        if Product.MDT in products:
            required.append(self.URL_FIELD_MDT)
        if Product.ORTO in products:
            required.append(self.URL_FIELD_ORTO)
        missing = [field_name for field_name in required if field_name not in names]
        if missing:
            raise RuntimeError(
                "A grade embutida não possui os campos de URL esperados: "
                + ", ".join(missing)
                + "."
            )

    def _activate_draw_tool(self):
        self._prev_map_tool = self.canvas.mapTool()

        def _done(rect):
            self._aoi_rect_map_crs = rect
            self._aoi_draw_crs = self.canvas.mapSettings().destinationCrs()

            if self._prev_map_tool is not None:
                self.canvas.setMapTool(self._prev_map_tool)
            self._prev_map_tool = None

            if self._draw_tool is not None:
                self._draw_tool.clear()

            self.cmbAoiMode.setCurrentText(self.AOI_DRAW)
            self._update_aoi_info()
            self._log("AOI definida pelo retângulo desenhado.")

        self._log("Desenhe um retângulo no mapa (clique e arraste)...")
        self._draw_tool = _ExtentTool(self.canvas, _done, log_fn=self._log)

        self.canvas.setFocus()
        self.canvas.setMapTool(self._draw_tool)

    def _get_aoi_geometry(self):
        mode = self.cmbAoiMode.currentText()

        if mode == self.AOI_CANVAS:
            return self.canvas.extent(), self.canvas.mapSettings().destinationCrs()

        if mode == self.AOI_LAYER:
            lyr = self._get_layer_by_combo(self.cmbLayer)
            if not lyr:
                return None, None
            return lyr.extent(), lyr.crs()

        if mode == self.AOI_DRAW:
            if self._aoi_rect_map_crs is None or self._aoi_draw_crs is None:
                return None, None
            return self._aoi_rect_map_crs, self._aoi_draw_crs

        return None, None

    def _update_aoi_info(self):
        rect, crs = self._get_aoi_geometry()
        if rect is None or crs is None:
            self.txtAoiInfo.setText("Nenhuma AOI definida.")
            return

        self.txtAoiInfo.setText(
            f"{crs.authid()} | xmin={rect.xMinimum():.6f}, ymin={rect.yMinimum():.6f}, "
            f"xmax={rect.xMaximum():.6f}, ymax={rect.yMaximum():.6f}"
        )

    def _selected_products(self):
        prods = []
        if self.chkMDE.isChecked():
            prods.append(Product.MDE)
        if self.chkMDT.isChecked():
            prods.append(Product.MDT)
        if self.chkORTO.isChecked():
            prods.append(Product.ORTO)
        return prods

    def _set_running(self, running: bool):
        self._running = running
        self.btnRun.setEnabled(not running)
        self.btnClose.setEnabled(not running)
        self.btnCancel.setEnabled(running)
        self.btnBrowse.setEnabled(not running)
        self.btnPickRect.setEnabled(not running)
        self.cmbAoiMode.setEnabled(not running)
        self.cmbLayer.setEnabled(not running)
        self.chkMDE.setEnabled(not running)
        self.chkMDT.setEnabled(not running)
        self.chkORTO.setEnabled(not running)

    def _cancel(self):
        if self._task is not None:
            self._log("Cancelamento solicitado. Encerrando downloads...")
            self._task.cancel()

    def _run(self):
        if self._running:
            return

        out_dir = self.txtOutDir.text().strip()
        if not out_dir:
            QMessageBox.warning(self, "PE3D-Downloader", "Escolha uma pasta de saída.")
            return
        os.makedirs(out_dir, exist_ok=True)

        products = self._selected_products()
        if not products:
            QMessageBox.warning(self, "PE3D-Downloader", "Marque ao menos um produto (MDE/MDT/ORTOFOTO).")
            return

        rect, crs = self._get_aoi_geometry()
        if rect is None or crs is None:
            QMessageBox.warning(self, "PE3D-Downloader", "Defina uma área de interesse.")
            return

        grid_lyr = self._embedded_grid
        if grid_lyr is None or not grid_lyr.isValid():
            QMessageBox.critical(
                self,
                "PE3D-Downloader",
                "A grade embutida não está disponível.\n"
                "Verifique se existe data/pe3d_grid.gpkg dentro da pasta do plugin."
            )
            return

        try:
            self._validate_url_fields_exist(grid_lyr, products)
        except Exception as ex:
            QMessageBox.critical(self, "PE3D-Downloader", str(ex))
            return

        self._settings.setValue("PE3DDownloader/out_dir", out_dir)

        self.txtLog.setPlainText("")
        self.progressBar.setValue(0)
        self.progressProduct.setValue(0)
        self.lblProductStatus.setText("Preparando seleção das quadrículas...")
        self._set_running(True)

        try:
            self._log("Selecionando quadrículas que intersectam a AOI (grade embutida)...")
            selector = GridSelector(self.iface)

            tiles = selector.tiles_in_aoi(
                grid_lyr, rect, crs,
                url_field_mde=self.URL_FIELD_MDE if Product.MDE in products else None,
                url_field_mdt=self.URL_FIELD_MDT if Product.MDT in products else None,
                url_field_orto=self.URL_FIELD_ORTO if Product.ORTO in products else None,
                mode="intersects"
            )

            if not tiles:
                self._log("Nenhuma quadrícula encontrada na AOI.")
                QMessageBox.information(self, "PE3D-Downloader", "Nenhuma quadrícula encontrada para a AOI.")
                self._set_running(False)
                return

            self._log(f"Quadrículas selecionadas: {len(tiles)} (por interseção com a AOI)")
            aoi_bounds = (
                rect.xMinimum(), rect.yMinimum(), rect.xMaximum(), rect.yMaximum()
            )
            self._task = PE3DDownloadTask(
                tiles,
                products,
                out_dir,
                aoi_bounds,
                crs.toWkt(),
                self._task_finished,
            )
            self._start_task(self._task)
        except Exception as ex:
            self._log(f"ERRO: {ex}")
            QMessageBox.critical(self, "PE3D-Downloader", str(ex))
            self._set_running(False)

    def _task_finished(self, task, result):
        keep_running = False
        try:
            if task.was_cancelled or task.isCanceled():
                completed_count = sum(len(items) for items in task.completed_downloads.values())
                self._log(
                    f"Operação cancelada. ZIPs completos disponíveis: {completed_count}."
                )
                if completed_count and not task.partial_only:
                    answer = QMessageBox.question(
                        self,
                        "PE3D-Downloader",
                        "Deseja extrair e carregar as imagens que já foram baixadas?",
                        QMessageBox.Yes | QMessageBox.No,
                        QMessageBox.Yes,
                    )
                    if answer == QMessageBox.Yes:
                        products = [
                            product
                            for product in task.products
                            if task.completed_downloads.get(product)
                        ]
                        partial_task = PE3DDownloadTask(
                            [],
                            products,
                            task.out_dir,
                            task.aoi_bounds,
                            task.aoi_srs_wkt,
                            self._task_finished,
                            completed_downloads=task.completed_downloads,
                            partial_only=True,
                            run_suffix=task.run_suffix,
                        )
                        self._task = partial_task
                        keep_running = True
                        self._log("Processando os downloads concluídos antes do cancelamento...")
                        self._start_task(partial_task)
                        return
            elif not result or task.error_message:
                message = task.error_message or "A tarefa terminou sem concluir o processamento."
                completed_count = sum(len(items) for items in task.completed_downloads.values())
                if completed_count and not task.partial_only:
                    answer = QMessageBox.question(
                        self,
                        "PE3D-Downloader — conexão interrompida",
                        f"{message}\n\nDeseja extrair e carregar os {completed_count} "
                        "arquivos concluídos antes da falha?",
                        QMessageBox.Yes | QMessageBox.No,
                        QMessageBox.Yes,
                    )
                    if answer == QMessageBox.Yes:
                        products = [
                            product for product in task.products
                            if task.completed_downloads.get(product)
                        ]
                        partial_task = PE3DDownloadTask(
                            [], products, task.out_dir, task.aoi_bounds, task.aoi_srs_wkt,
                            self._task_finished,
                            completed_downloads=task.completed_downloads,
                            partial_only=True,
                            run_suffix=task.run_suffix,
                        )
                        self._task = partial_task
                        keep_running = True
                        self._start_task(partial_task)
                        return
                else:
                    QMessageBox.critical(self, "PE3D-Downloader", message)
            else:
                for vrt_path, layer_name in task.created_vrts:
                    raster = QgsRasterLayer(vrt_path, layer_name)
                    if raster.isValid():
                        QgsProject.instance().addMapLayer(raster)
                    else:
                        self._log(f"Raster inválido ao carregar VRT: {vrt_path}")
                QMessageBox.information(
                    self,
                    "PE3D-Downloader",
                    f"Concluído. VRTs recortados criados: {len(task.created_vrts)}",
                )
        finally:
            if not keep_running:
                self._task = None
                self._set_running(False)

    def _start_task(self, task):
        task.log_message.connect(self._log)
        task.progressChanged.connect(self._set_progress)
        task.product_progress.connect(self.progressProduct.setValue)
        task.status_message.connect(self.lblProductStatus.setText)
        QgsApplication.taskManager().addTask(task)

    def _set_progress(self, v):
        self.progressBar.setValue(max(0, min(100, int(v))))
