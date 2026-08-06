# -*- coding: utf-8 -*-
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import zipfile
from enum import Enum
from urllib.parse import unquote, urlparse

import requests
from osgeo import gdal
from requests.exceptions import RequestException, SSLError


class Product(Enum):
    MDE = "MDE"
    MDT = "MDT"
    ORTO = "ORTOFOTO"


class CancelledError(RuntimeError):
    pass


class PE3DDownloader:
    """Download, extração segura e criação atômica de VRTs."""

    _KNOWN_INSECURE_HOSTS = set()
    _WARNED_INSECURE_HOSTS = set()

    MAX_DOWNLOAD_BYTES = 20 * 1024 ** 3       # 20 GiB por ZIP
    MAX_EXTRACTED_BYTES = 60 * 1024 ** 3      # 60 GiB por ZIP
    MAX_ARCHIVE_MEMBERS = 20000
    MAX_COMPRESSION_RATIO = 1000

    def __init__(self, log_fn, progress_fn=None, timeout_s=180):
        self.log = log_fn
        self.progress = progress_fn or (lambda value: None)
        self.timeout_s = timeout_s
        self.session = requests.Session()
        self._cancelled = False
        self._insecure_hosts = self._KNOWN_INSECURE_HOSTS
        self._ssl_warning_hosts = self._WARNED_INSECURE_HOSTS

    def close(self):
        self.session.close()

    def cancel(self):
        self._cancelled = True

    def _check_cancel(self):
        if self._cancelled:
            raise CancelledError("Operação cancelada pelo usuário.")

    def _try_head(self, url, verify=True):
        try:
            self._check_cancel()
            response = self.session.head(
                url, timeout=(15, 30), allow_redirects=True, verify=verify
            )
            if response.status_code >= 400 and response.status_code not in (403, 405):
                self.log(f"AVISO: servidor respondeu HTTP {response.status_code} ao HEAD: {response.url}")
            response.close()
            return True
        except CancelledError:
            raise
        except RequestException as ex:
            self.log(f"AVISO: não foi possível consultar o arquivo com HEAD: {ex}")
            return None

    def _sha256(self, path):
        digest = hashlib.sha256()
        with open(path, "rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                self._check_cancel()
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _metadata_path(zip_path):
        return zip_path + ".download.json"

    def _write_download_metadata(self, zip_path, url, sha256):
        metadata_path = self._metadata_path(zip_path)
        temp_path = metadata_path + ".tmp"
        data = {
            "url": url,
            "size": os.path.getsize(zip_path),
            "sha256": sha256,
            "mtime_ns": os.stat(zip_path).st_mtime_ns,
        }
        with open(temp_path, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
        os.replace(temp_path, metadata_path)

    def _existing_zip_is_valid(self, zip_path, url):
        metadata_path = self._metadata_path(zip_path)
        if not os.path.isfile(zip_path) or not os.path.isfile(metadata_path):
            return False
        try:
            with open(metadata_path, "r", encoding="utf-8") as stream:
                metadata = json.load(stream)
            if metadata.get("url") != url:
                self.log("Cache ignorado: a URL registrada é diferente da URL atual.")
                return False
            if metadata.get("size") != os.path.getsize(zip_path):
                self.log("Cache ignorado: o tamanho do ZIP foi alterado.")
                return False
            # Arquivos gerados pelo plugin e não modificados não precisam ser
            # relidos integralmente a cada execução. Se o mtime mudou, o SHA é
            # recalculado para confirmar a integridade.
            if metadata.get("mtime_ns") != os.stat(zip_path).st_mtime_ns:
                digest = self._sha256(zip_path)
                if metadata.get("sha256") != digest:
                    self.log("Cache ignorado: o checksum SHA-256 não confere.")
                    return False
                metadata["mtime_ns"] = os.stat(zip_path).st_mtime_ns
                temp_path = metadata_path + ".tmp"
                with open(temp_path, "w", encoding="utf-8") as stream:
                    json.dump(metadata, stream, ensure_ascii=False, indent=2)
                os.replace(temp_path, metadata_path)
            with zipfile.ZipFile(zip_path, "r"):
                pass
            return True
        except CancelledError:
            raise
        except (OSError, ValueError, zipfile.BadZipFile, json.JSONDecodeError) as ex:
            self.log(f"Cache ignorado: falha na validação do ZIP ({ex}).")
            return False

    def _download_stream(self, url, out_zip_path, verify=True):
        self._check_cancel()
        temp_path = out_zip_path + ".part"
        digest = hashlib.sha256()
        existing = os.path.getsize(temp_path) if os.path.exists(temp_path) else 0
        headers = {"Range": f"bytes={existing}-"} if existing else {}
        try:
            with self.session.get(
                url,
                stream=True,
                timeout=(15, self.timeout_s),
                allow_redirects=True,
                verify=verify,
                headers=headers,
            ) as response:
                response.raise_for_status()
                resumed = existing > 0 and response.status_code == 206
                if resumed:
                    with open(temp_path, "rb") as previous:
                        for previous_chunk in iter(lambda: previous.read(1024 * 1024), b""):
                            self._check_cancel()
                            digest.update(previous_chunk)
                    mode = "ab"
                    done = existing
                    total = existing + int(response.headers.get("Content-Length", "0") or "0")
                    self.log(f"Retomando download a partir de {existing / 1024 ** 2:.1f} MiB.")
                else:
                    mode = "wb"
                    done = 0
                    total = int(response.headers.get("Content-Length", "0") or "0")
                if total > self.MAX_DOWNLOAD_BYTES:
                    raise RuntimeError(
                        f"Download recusado: {total / 1024 ** 3:.2f} GiB excede o limite de "
                        f"{self.MAX_DOWNLOAD_BYTES / 1024 ** 3:.0f} GiB."
                    )
                with open(temp_path, mode) as stream:
                    for chunk in response.iter_content(chunk_size=1024 * 256):
                        self._check_cancel()
                        if not chunk:
                            continue
                        done += len(chunk)
                        if done > self.MAX_DOWNLOAD_BYTES:
                            raise RuntimeError("Download excedeu o limite máximo permitido.")
                        stream.write(chunk)
                        digest.update(chunk)
                        if total > 0:
                            self.progress(min(70, int(done / total * 70)))
            os.replace(temp_path, out_zip_path)
            return digest.hexdigest()
        except CancelledError:
            # Mantém o parcial para uma futura retomada.
            raise
        except RuntimeError:
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except OSError as cleanup_error:
                self.log(f"AVISO: não foi possível remover download temporário: {cleanup_error}")
            raise

    def _download_file(self, url, out_zip_path):
        self._check_cancel()
        host = urlparse(url).netloc.lower()
        last_error = None
        for attempt in range(1, 4):
            self._check_cancel()
            verify = host not in self._insecure_hosts
            try:
                digest = self._download_stream(url, out_zip_path, verify=verify)
                try:
                    with zipfile.ZipFile(out_zip_path, "r"):
                        pass
                except zipfile.BadZipFile as ex:
                    last_error = ex
                    self.log("ZIP recebido está inválido; reiniciando o download do zero.")
                    if os.path.exists(out_zip_path):
                        os.remove(out_zip_path)
                    continue
                self._write_download_metadata(out_zip_path, url, digest)
                return
            except SSLError as ex:
                self._insecure_hosts.add(host)
                last_error = ex
                if host not in self._ssl_warning_hosts:
                    self._ssl_warning_hosts.add(host)
                    self.log(
                        "AVISO: o servidor PE3D exige conexão sem validação SSL. "
                        "Os próximos arquivos usarão esse modo diretamente."
                    )
                continue
            except RequestException as ex:
                last_error = ex
                if attempt < 3:
                    wait_seconds = 2 ** (attempt - 1)
                    self.log(
                        f"Conexão interrompida ({ex}). Nova tentativa {attempt + 1}/3 "
                        f"em {wait_seconds}s; o download parcial será retomado."
                    )
                    for _ in range(wait_seconds * 5):
                        self._check_cancel()
                        time.sleep(0.2)
                    continue
        raise RuntimeError(
            f"Não foi possível concluir o download após 3 tentativas: {last_error}"
        ) from last_error

    def _validate_archive(self, archive):
        members = archive.infolist()
        if len(members) > self.MAX_ARCHIVE_MEMBERS:
            raise RuntimeError(f"ZIP recusado: contém {len(members)} entradas.")
        total = sum(info.file_size for info in members)
        if total > self.MAX_EXTRACTED_BYTES:
            raise RuntimeError(
                f"ZIP recusado: tamanho extraído estimado em {total / 1024 ** 3:.2f} GiB."
            )
        for info in members:
            compressed = max(info.compress_size, 1)
            if info.file_size / compressed > self.MAX_COMPRESSION_RATIO:
                raise RuntimeError(f"ZIP recusado: taxa de compressão suspeita em '{info.filename}'.")
        return members, total

    def _extract_safely(self, zip_path, extract_dir):
        parent = os.path.dirname(extract_dir)
        temp_dir = tempfile.mkdtemp(prefix=os.path.basename(extract_dir) + "_tmp_", dir=parent)
        backup_dir = extract_dir + ".old"
        try:
            with zipfile.ZipFile(zip_path, "r") as archive:
                members, total = self._validate_archive(archive)
                free_space = shutil.disk_usage(temp_dir).free
                if total > free_space:
                    raise RuntimeError(
                        f"Espaço insuficiente: o ZIP requer até {total / 1024 ** 3:.2f} GiB "
                        f"e há {free_space / 1024 ** 3:.2f} GiB livres."
                    )
                extracted = 0
                root_real = os.path.realpath(temp_dir)
                for info in members:
                    self._check_cancel()
                    relative = info.filename.replace("\\", "/")
                    target = os.path.realpath(os.path.join(temp_dir, relative))
                    if os.path.commonpath((root_real, target)) != root_real:
                        raise RuntimeError(f"Caminho inseguro dentro do ZIP: {info.filename}")
                    if info.is_dir():
                        os.makedirs(target, exist_ok=True)
                        continue
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with archive.open(info, "r") as source, open(target, "wb") as destination:
                        while True:
                            self._check_cancel()
                            chunk = source.read(1024 * 1024)
                            if not chunk:
                                break
                            destination.write(chunk)
                            extracted += len(chunk)
                            if extracted > self.MAX_EXTRACTED_BYTES:
                                raise RuntimeError("Extração excedeu o limite máximo permitido.")
                            if total:
                                self.progress(70 + min(29, int(extracted / total * 29)))

            if os.path.exists(backup_dir):
                shutil.rmtree(backup_dir)
            if os.path.exists(extract_dir):
                os.replace(extract_dir, backup_dir)
            try:
                os.replace(temp_dir, extract_dir)
            except Exception:
                if os.path.exists(backup_dir) and not os.path.exists(extract_dir):
                    os.replace(backup_dir, extract_dir)
                raise
            if os.path.exists(backup_dir):
                shutil.rmtree(backup_dir)
            self.progress(100)
        finally:
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)

    def download_by_url(self, product, fid, url, out_dir):
        """Garante um ZIP completo no cache e retorna seus dados de trabalho."""
        self._check_cancel()
        parsed = urlparse(str(url).strip())
        if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
            raise RuntimeError(f"URL inválida para {product.value} (fid={fid}): '{url}'")

        prod_dir = os.path.join(out_dir, product.value)
        os.makedirs(prod_dir, exist_ok=True)
        base = os.path.basename(unquote(parsed.path)) or f"{product.value}.zip"
        base = re.sub(r'[^A-Za-z0-9._-]+', '_', base)
        if not base.lower().endswith(".zip"):
            base += ".zip"
        zip_path = os.path.join(prod_dir, f"fid_{fid}_{base}")

        if self._existing_zip_is_valid(zip_path, url):
            self.log(f"ZIP validado no cache: {zip_path}")
        else:
            self.log(f"Baixando: {url}")
            self._download_file(url, zip_path)
        self.progress(100)

        with open(self._metadata_path(zip_path), "r", encoding="utf-8") as stream:
            metadata = json.load(stream)
        return {
            "product": product,
            "fid": fid,
            "url": url,
            "zip_path": zip_path,
            "extract_dir": os.path.join(prod_dir, f"fid_{fid}"),
            "sha256": metadata["sha256"],
        }

    def extract_downloaded(self, download):
        """Extrai um download completo ou reutiliza uma extração validada."""
        self._check_cancel()
        zip_path = download["zip_path"]
        extract_dir = download["extract_dir"]
        manifest_path = os.path.join(extract_dir, ".pe3d-extract.json")
        try:
            with open(manifest_path, "r", encoding="utf-8") as stream:
                manifest = json.load(stream)
            if manifest.get("zip_sha256") == download["sha256"]:
                tifs = [
                    os.path.join(extract_dir, relative)
                    for relative in manifest.get("tifs", [])
                ]
                if tifs and all(os.path.isfile(path) for path in tifs):
                    self.log(f"Extração validada no cache: fid={download['fid']}")
                    self.progress(100)
                    return tifs
        except (OSError, ValueError, json.JSONDecodeError):
            manifest = None

        self._check_cancel()
        self.log(f"Extraindo com validação: {zip_path}")
        self._extract_safely(zip_path, extract_dir)

        tifs = []
        for current_root, _, files in os.walk(extract_dir):
            self._check_cancel()
            for filename in files:
                if filename.lower().endswith((".tif", ".tiff")):
                    tifs.append(os.path.join(current_root, filename))
        if not tifs:
            product = download["product"]
            raise RuntimeError(
                f"Nenhum TIFF encontrado após extração ({product.value} fid={download['fid']})."
            )
        tifs = sorted(tifs)
        manifest = {
            "zip_sha256": download["sha256"],
            "tifs": [os.path.relpath(path, extract_dir) for path in tifs],
        }
        temp_manifest = manifest_path + ".tmp"
        with open(temp_manifest, "w", encoding="utf-8") as stream:
            json.dump(manifest, stream, ensure_ascii=False, indent=2)
        os.replace(temp_manifest, manifest_path)
        return tifs

    def download_and_extract_by_url(self, product, fid, url, out_dir):
        """Compatibilidade com chamadas antigas."""
        return self.extract_downloaded(self.download_by_url(product, fid, url, out_dir))

    def build_clipped_vrt(self, vrt_path, tif_paths, aoi_bounds, aoi_srs_wkt):
        """Cria mosaico persistente e um VRT recortado que o referencia."""
        self._check_cancel()
        output_dir = os.path.dirname(vrt_path)
        os.makedirs(output_dir, exist_ok=True)
        base_path, _ = os.path.splitext(vrt_path)
        mosaic_path = base_path + ".source.vrt"
        mosaic_temp = mosaic_path + ".tmp.vrt"
        clipped_path = vrt_path + ".tmp.vrt"

        def callback(complete, _message, _data):
            self.progress(int(complete * 100))
            return 0 if self._cancelled else 1

        for temporary in (mosaic_temp, clipped_path):
            if os.path.exists(temporary):
                os.remove(temporary)
        try:
            mosaic = gdal.BuildVRT(
                mosaic_temp,
                tif_paths,
                options=gdal.BuildVRTOptions(callback=callback),
            )
            if mosaic is None:
                self._check_cancel()
                raise RuntimeError("gdal.BuildVRT não conseguiu criar o mosaico completo.")
            mosaic.FlushCache()
            mosaic = None
            self._check_cancel()
            os.replace(mosaic_temp, mosaic_path)

            clipped = gdal.Warp(
                clipped_path,
                mosaic_path,
                format="VRT",
                outputBounds=aoi_bounds,
                outputBoundsSRS=aoi_srs_wkt,
                callback=callback,
            )
            if clipped is None:
                self._check_cancel()
                raise RuntimeError("gdal.Warp não conseguiu recortar o mosaico pela AOI.")
            clipped.FlushCache()
            clipped = None
            self._check_cancel()
            os.replace(clipped_path, vrt_path)
        finally:
            for temporary in (mosaic_temp, clipped_path):
                try:
                    if os.path.exists(temporary):
                        os.remove(temporary)
                except OSError as cleanup_error:
                    self.log(f"AVISO: não foi possível remover VRT temporário: {cleanup_error}")
