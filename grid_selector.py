# -*- coding: utf-8 -*-
from qgis.core import (
    QgsCoordinateTransform,
    QgsProject,
    QgsGeometry,
    QgsFeatureRequest,
)

class GridSelector:
    def __init__(self, iface):
        self.iface = iface

    def tiles_in_aoi(
        self,
        grid_layer,
        aoi_rect,
        aoi_crs,
        url_field_mde="MDE",
        url_field_mdt="MDT",
        url_field_orto="ORTOFOTO",
        mode="intersects",  # "intersects" ou "within"
    ):
        """
        Retorna lista de dicts (1 por feição selecionada):
        [
          {"fid": 123, "mde": "http://...", "mdt": "http://...", "orto": "http://..."},
          ...
        ]

        mode:
          - "intersects": pega qualquer quadrícula que INTERSECTA a AOI (o mais comum)
          - "within": pega apenas quadrículas COMPLETAMENTE DENTRO da AOI
        """
        if grid_layer is None:
            return []

        # valida campos de URL
        field_names = {f.name() for f in grid_layer.fields()}
        requested_fields = [
            field_name
            for field_name in (url_field_mde, url_field_mdt, url_field_orto)
            if field_name
        ]
        missing = [field_name for field_name in requested_fields if field_name not in field_names]
        if missing:
            raise RuntimeError(
                "A camada de quadrículas não possui os campos de URL esperados: "
                + ", ".join(missing)
                + ". Esperado: MDE, MDT, ORTOFOTO."
            )

        # AOI geom
        grid_crs = grid_layer.crs()
        aoi_geom = QgsGeometry.fromRect(aoi_rect)

        # transforma AOI para CRS da camada de grade
        if grid_crs != aoi_crs:
            xform = QgsCoordinateTransform(aoi_crs, grid_crs, QgsProject.instance())
            aoi_geom.transform(xform)

        aoi_bbox = aoi_geom.boundingBox()

        # Pré-filtro rápido por retângulo (muito mais eficiente em camadas grandes)
        req = QgsFeatureRequest().setFilterRect(aoi_bbox)

        tiles = []
        for f in grid_layer.getFeatures(req):
            g = f.geometry()
            if not g or g.isEmpty():
                continue

            if mode == "within":
                ok = g.within(aoi_geom)  # feição totalmente dentro da AOI
            else:
                ok = g.intersects(aoi_geom)  # qualquer interseção

            if not ok:
                continue

            def _val(field_name):
                if not field_name:
                    return ""
                v = f[field_name]
                if v is None:
                    return ""
                s = str(v).strip()
                return s

            tiles.append({
                "fid": int(f.id()),
                "mde": _val(url_field_mde),
                "mdt": _val(url_field_mdt),
                "orto": _val(url_field_orto),
                # Ordem de leitura: linhas do norte para o sul e, em cada
                # linha, do oeste para o leste.
                "grid_x": g.boundingBox().center().x(),
                "grid_y": g.boundingBox().center().y(),
            })

        # remove duplicados por fid
        seen = set()
        out = []
        for t in tiles:
            if t["fid"] in seen:
                continue
            seen.add(t["fid"])
            out.append(t)

        return sorted(out, key=lambda item: (-item["grid_y"], item["grid_x"], item["fid"]))
