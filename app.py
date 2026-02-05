"""
Proyecto: Bases de datos espaciales (PostGIS) + Visualización (Folium) con GUI (PySide6)

Este script implementa una aplicación de escritorio que:

1) Descarga un shapefile (ZIP) desde una URL durante la ejecución.
2) Lee el shapefile con GeoPandas.
3) Normaliza la proyección para visualización (EPSG:4326) y sube a PostGIS.
4) Permite filtrar por categorías de “zonas verdes” ('forest' y 'nature_reserve').
5) Calcula áreas en hectáreas con EPSG:25830 (métrico) y redondea a 1 decimal.
6) Genera un mapa interactivo (Leaflet vía Folium) embebido en un QWebEngineView.
7) Muestra tooltips con fclass, name y superficie (ha) por polígono + superficie total filtrada.

Requisitos del entorno:
- PostgreSQL local con credenciales según config
- Extensión PostGIS disponible (se crea si no existe)
- Librerías: PySide6, PySide6-WebEngine, geopandas, sqlalchemy, requests, folium, certifi

Importante:
- Toda la funcionalidad reside en un único fichero .py (tal como se suele pedir).
"""

import sys
import os
import json
import tempfile
import zipfile
from pathlib import Path  # (Se conserva por compatibilidad; no es estrictamente necesario)

# -------------------- Qt / PySide6 --------------------
from PySide6.QtCore import Qt, QUrl, QSize
from PySide6.QtWidgets import (
    QApplication,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QCheckBox,
    QTextEdit,
    QMessageBox,
    QSplitter,
    QProgressDialog,
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEngineSettings

# -------------------- GIS / BD / Web --------------------
import requests
import certifi
import folium
from branca.element import MacroElement, Template
import geopandas as gpd
from sqlalchemy import create_engine, text


# =============================================================================
# CONFIGURACIÓN (constantes del proyecto)
# =============================================================================

# Centro y zoom inicial del mapa (se ajustará luego al bbox si hay datos)
ANDALUCIA_CENTER = (37.4, -4.5)
ANDALUCIA_ZOOM = 7

# URL del ZIP con shapefile (se descarga en tiempo de ejecución)
ZIP_URL = "https://www.uhu.es/jluis.dominguez/AGI/andalucia-landuse.shp.zip"

# Parámetros de conexión PostgreSQL/PostGIS
PG_USER = "postgres"
PG_PASS = "postgres"
PG_HOST = "localhost"
PG_PORT = 5432
PG_DB = "nyc"

# URLs SQLAlchemy: una para administración (crear BD) y otra para la BD objetivo
PG_URL_DB = f"postgresql://{PG_USER}:{PG_PASS}@{PG_HOST}:{PG_PORT}/{PG_DB}"
PG_URL_ADMIN = f"postgresql://{PG_USER}:{PG_PASS}@{PG_HOST}:{PG_PORT}/postgres"

# Tabla donde se guardan los datos (se reemplaza cada vez que se carga)
TABLE_NAME = "ANDALUCIA_USOS_SUELO"

# Colores por categoría (fclass) para el estilo del mapa
FCLASS_COLORS = {
    "forest": "#1b9e77",
    "nature_reserve": "#66a61e",
}

# Categorías objetivo de “zonas verdes” según enunciado
TARGET_FCLASSES = ("forest", "nature_reserve")


# =============================================================================
# APLICACIÓN (GUI)
# =============================================================================

class App(QWidget):
    """
    Ventana principal.

    Diseñada como una sola clase para simplificar la entrega en un fichero único.
    Mantiene un "estado en memoria" (GeoJSON y bbox) para renderizar el mapa tras filtrar.
    """

    def __init__(self):
        super().__init__()

        # --- Propiedades básicas de la ventana
        self.setWindowTitle("Mapa de Andalucía")
        self.resize(1100, 750)

        # --- Estado en memoria para la visualización
        # geojson: dict que representa las features filtradas y con propiedades extras (area_ha)
        self.geojson: dict | None = None

        # bbox (minx, miny, maxx, maxy) en EPSG:4326, usado para hacer fit_bounds en el mapa
        self._bbox: tuple[float, float, float, float] | None = None

        # Construcción UI
        self._build_ui()

        # Mensaje inicial
        self.log_msg("Aplicación lista. Pulsa «Cargar datos» y luego «Visualizar».")

    # -------------------------------------------------------------------------
    # UI: construcción de widgets
    # -------------------------------------------------------------------------
    def _build_ui(self):
        """
        Construye la interfaz:
        - Fila de botones: cargar datos / visualizar
        - Fila de filtros (checkboxes)
        - Splitter vertical: mapa arriba + log abajo
        """
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        # --- Botones principales
        fila_btns = QHBoxLayout()
        self.btn_cargar = QPushButton("Cargar datos")
        self.btn_ver = QPushButton("Visualizar")

        # Conexión señales -> slots (acciones)
        self.btn_cargar.clicked.connect(self.cargar_datos_postgis)
        self.btn_ver.clicked.connect(self.visualizar)

        fila_btns.addWidget(self.btn_cargar)
        fila_btns.addWidget(self.btn_ver)
        fila_btns.addStretch(1)
        root.addLayout(fila_btns)

        # --- Checkboxes de filtro (forest / nature_reserve)
        # Si no se selecciona ninguno, se mostrarán ambos (comportamiento requerido)
        fila_checks = QHBoxLayout()
        self.chk_forest = QCheckBox("forest")
        self.chk_nature = QCheckBox("nature_reserve")
        fila_checks.addWidget(self.chk_forest)
        fila_checks.addWidget(self.chk_nature)
        fila_checks.addStretch(1)
        root.addLayout(fila_checks)

        # --- Zona central: visor HTML (mapa) + log
        self.splitter = QSplitter(Qt.Vertical)

        # Visor web para renderizar HTML local generado por Folium
        self.view = QWebEngineView()
        self.view.setMinimumSize(QSize(400, 300))

        # Ajustes para permitir cargar contenido local con dependencias embebidas
        # (Folium puede incrustar recursos Leaflet, etc.)
        settings = self.view.settings()
        settings.setAttribute(QWebEngineSettings.LocalContentCanAccessRemoteUrls, True)
        settings.setAttribute(QWebEngineSettings.LocalContentCanAccessFileUrls, True)

        self.splitter.addWidget(self.view)

        # Log de texto para mensajes de estado (información al usuario)
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(60)
        self.log.setMaximumHeight(80)
        self.splitter.addWidget(self.log)

        # Reparto de espacio: el mapa ocupa casi todo
        self.splitter.setStretchFactor(0, 10)
        self.splitter.setStretchFactor(1, 1)

        root.addWidget(self.splitter)

    # -------------------------------------------------------------------------
    # Utilidad: log
    # -------------------------------------------------------------------------
    def log_msg(self, msg: str):
        """Añade una línea al log de la aplicación."""
        self.log.append(f"• {msg}")

    # =============================================================================
    # 1) CARGA: Descargar ZIP, leer shapefile y subir a PostGIS
    # =============================================================================
    def cargar_datos_postgis(self):
        """
        Flujo completo de carga:

        A) Asegurar existencia de la base de datos 'nyc' (si no existe, se crea).
        B) Asegurar extensión PostGIS.
        C) Descargar ZIP del shapefile desde la URL.
        D) Descomprimir y localizar el .shp.
        E) Leer shapefile con GeoPandas.
        F) Reproyectar a EPSG:4326 si hiciera falta (visualización web estándar).
        G) Filtrar columnas a {fclass, name, geometry}.
        H) Subir a PostGIS reemplazando tabla.
        """
        dlg = None
        try:
            # ---------------- Progress dialog ----------------
            # Se usa QProgressDialog para dar feedback durante un proceso largo:
            # - crear BD/extension
            # - descarga
            # - lectura GIS
            # - subida a PostGIS
            dlg = QProgressDialog("Preparando PostgreSQL/PostGIS…", None, 0, 100, self)
            dlg.setWindowTitle("Cargando datos…")
            dlg.setCancelButton(None)  # sin botón cancelar (flujo simple)
            dlg.setWindowModality(Qt.ApplicationModal)
            dlg.setMinimumDuration(0)
            dlg.setValue(5)
            QApplication.processEvents()

            # ---------------- A) Crear BD si no existe ----------------
            # Conexión a la BD "postgres" para tareas administrativas.
            # isolation_level AUTOCOMMIT: requerido para CREATE DATABASE.
            admin_engine = create_engine(PG_URL_ADMIN, isolation_level="AUTOCOMMIT")
            with admin_engine.connect() as conn:
                existe = conn.execute(
                    text("SELECT 1 FROM pg_database WHERE datname = :db"),
                    {"db": PG_DB},
                ).fetchone()
                if existe is None:
                    conn.execute(text(f'CREATE DATABASE "{PG_DB}"'))

            # ---------------- B) Asegurar PostGIS ----------------
            engine = create_engine(PG_URL_DB)
            with engine.connect() as conn:
                conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
                conn.commit()

            dlg.setLabelText("Descargando shapefile…")
            dlg.setValue(15)
            QApplication.processEvents()

            # ---------------- C) Descarga del ZIP con progreso ----------------
            # Guardamos en el directorio temporal del sistema.
            ruta_zip = os.path.join(tempfile.gettempdir(), "andalucia-landuse.shp.zip")

            # verify=certifi.where() para validar certificados con bundle actualizado.
            # Si ocurre un SSLError, reintentamos con verify=False (modo tolerante).
            try:
                resp = requests.get(ZIP_URL, stream=True, timeout=120, verify=certifi.where())
                resp.raise_for_status()
            except requests.exceptions.SSLError:
                resp = requests.get(ZIP_URL, stream=True, timeout=120, verify=False)
                resp.raise_for_status()

            total = int(resp.headers.get("Content-Length", 0))
            descargado = 0

            # Descarga por chunks para no cargar todo en memoria y poder actualizar progreso.
            with open(ruta_zip, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if not chunk:
                        continue
                    f.write(chunk)

                    if total > 0:
                        descargado += len(chunk)
                        # Progreso aproximado: 15..60 reservado a la descarga
                        pct = 15 + int(45 * (descargado / total))
                        dlg.setValue(min(60, pct))
                        QApplication.processEvents()

            if not os.path.exists(ruta_zip):
                raise RuntimeError("No se pudo guardar el ZIP descargado.")

            dlg.setLabelText("Descomprimiendo…")
            dlg.setValue(62)
            QApplication.processEvents()

            # ---------------- D) Descomprimir y localizar el .shp ----------------
            tmpdir = tempfile.mkdtemp(prefix="landuse_")
            with zipfile.ZipFile(ruta_zip, "r") as z:
                z.extractall(tmpdir)

            shp_files = [
                os.path.join(tmpdir, f)
                for f in os.listdir(tmpdir)
                if f.lower().endswith(".shp")
            ]
            if len(shp_files) == 0:
                raise RuntimeError("No se encontró ningún .shp en el ZIP.")
            shp_path = shp_files[0]  # tomamos el primero

            dlg.setLabelText("Leyendo shapefile…")
            dlg.setValue(72)
            QApplication.processEvents()

            # ---------------- E) Leer shapefile con GeoPandas ----------------
            gdf = gpd.read_file(shp_path)

            dlg.setLabelText("Reproyectando…")
            dlg.setValue(78)
            QApplication.processEvents()

            # ---------------- F) Reproyectar a EPSG:4326 para visualización ----------------
            # Folium/Leaflet trabaja en lat/lon WGS84.
            if gdf.crs is not None and gdf.crs.to_epsg() != 4326:
                gdf = gdf.to_crs(epsg=4326)

            dlg.setLabelText("Preparando columnas…")
            dlg.setValue(85)
            QApplication.processEvents()

            # ---------------- G) Mantener solo columnas requeridas ----------------
            
            cols = [c for c in gdf.columns if c in ("fclass", "name", "geometry")]
            if "fclass" not in cols or "geometry" not in cols:
                raise RuntimeError("Faltan columnas requeridas: fclass/geometry.")

            # 'name' puede no existir; si falta, se crea con None para cumplir esquema.
            if "name" not in cols:
                gdf["name"] = None
                cols = ["fclass", "name", "geometry"]

            gdf = gdf[cols]

            dlg.setLabelText("Subiendo a PostGIS…")
            dlg.setValue(92)
            QApplication.processEvents()

            # ---------------- H) Subida a PostGIS ----------------
            # replace: elimina y recrea la tabla en cada carga.
            gdf.to_postgis(TABLE_NAME, engine, if_exists="replace", index=False)

            dlg.setLabelText("Finalizando…")
            dlg.setValue(100)
            QApplication.processEvents()
            dlg.close()

            # ÚNICO log permitido aquí (según tu propia regla en el código original)
            self.log_msg("✅ Base de datos actualizada")

        except Exception as e:
            # Intentar cerrar diálogo si existe
            try:
                if dlg is not None:
                    dlg.close()
            except Exception:
                pass
            QMessageBox.critical(self, "Error", str(e))

    # =============================================================================
    # 2) LECTURA + FILTRO: PostGIS -> GeoDataFrame -> GeoJSON con áreas
    # =============================================================================
    def _leer_y_filtrar(self) -> bool:
        """
        Lee desde PostGIS aplicando filtros según checkboxes.

        - Si forest está marcado: incluir forest
        - Si nature_reserve está marcado: incluir nature_reserve
        - Si no hay nada marcado: incluir ambos

        Tras leer:
        - Asegura CRS 4326 para mapa
        - Calcula area_ha usando EPSG:25830 (métrico) y la copia reproyectada
        - Guarda bbox y geojson en memoria para su renderizado posterior
        - Calcula y muestra superficie total en ha (en el log)
        """
        try:
            engine = create_engine(PG_URL_DB)

            # ---------------- Construcción de filtros ----------------
            filtros: list[str] = []
            if self.chk_forest.isChecked():
                filtros.append("'forest'")
            if self.chk_nature.isChecked():
                filtros.append("'nature_reserve'")

            # Si el usuario no marca nada: comportamiento requerido -> mostrar todo (ambos)
            if len(filtros) == 0:
                filtros = [f"'{c}'" for c in TARGET_FCLASSES]

            # ---------------- Query SQL ----------------
            # NOTA: Usamos comillas dobles en el nombre de tabla por si hay mayúsculas.
            sql = f"""
                SELECT fclass, name, geometry
                FROM "{TABLE_NAME}"
                WHERE fclass IN ({",".join(filtros)})
            """

            # read_postgis convierte a GeoDataFrame, tomando geometry como geom_col.
            gdf = gpd.read_postgis(sql, engine, geom_col="geometry")

            # ---------------- Validación básica ----------------
            if gdf is None or gdf.empty:
                self.log_msg("⚠️ No hay datos para el filtro seleccionado.")
                self.geojson = None
                self._bbox = None
                return False

            # ---------------- CRS para visualización ----------------
            # Folium/Leaflet requiere EPSG:4326 para lat/lon.
            if gdf.crs is not None and gdf.crs.to_epsg() != 4326:
                gdf = gdf.to_crs(epsg=4326)

            # ---------------- Cálculo de superficies (SRID 25830) ----------------
            # La superficie no se debe calcular en 4326 (grados).
            # Por eso hacemos una copia reproyectada a EPSG:25830:
            # - unidad en metros -> área en m² -> se convierte a hectáreas.
            gdf_utm = gdf.to_crs(epsg=25830)
            gdf["area_ha"] = (gdf_utm.geometry.area / 10000).round(1)  # m² -> ha
            total_ha = float(gdf["area_ha"].sum().round(1))

            # ---------------- Guardar bbox y GeoJSON para el mapa ----------------
            self._bbox = tuple(gdf.total_bounds)  # (minx, miny, maxx, maxy)
            self.geojson = json.loads(gdf.to_json())

            # Logs informativos
            self.log_msg(f"Datos filtrados: {len(gdf)} registros.")
            self.log_msg(f"Superficie total: {total_ha} ha")
            return True

        except Exception as e:
            QMessageBox.critical(self, "Error al leer BD", str(e))
            self.log_msg(f"Error al leer PostGIS: {e}")
            self.geojson = None
            self._bbox = None
            return False

    # =============================================================================
    # 3) MAPA: estilos, leyenda y construcción Folium
    # =============================================================================
    def _style_by_fclass(self, feature: dict) -> dict:
        """
        Devuelve un estilo de dibujo para cada feature según su 'fclass'.

        Folium/Leaflet llama a style_function para cada feature del GeoJSON.
        Devolvemos:
        - color / fillColor: el color del contorno y relleno
        - weight: grosor del contorno
        - fillOpacity: transparencia del relleno
        """
        props = feature.get("properties") or {}
        fclass = props.get("fclass")
        color = FCLASS_COLORS.get(fclass, "#999999")  # gris si es desconocido
        return {"color": color, "fillColor": color, "weight": 1, "fillOpacity": 0.3}

    def _legend_control(self) -> Template:
        """
        Crea un HTML fijo (div flotante) con la leyenda del mapa.

        Se inyecta como MacroElement en el root del mapa Folium.
        """
        items_html = "".join(
            f'<div style="margin-bottom:4px">'
            f'<span style="display:inline-block;width:12px;height:12px;background:{col};'
            f'margin-right:6px;border:1px solid #444"></span>'
            f'<span style="font-size:12px">{cls}</span></div>'
            for cls, col in FCLASS_COLORS.items()
        )

        html = f"""
        <div style="position: fixed; bottom: 18px; right: 18px; background: white;
        border: 1px solid #bbb; padding: 8px 10px; border-radius: 6px;
        box-shadow: 0 1px 4px rgba(0,0,0,0.3); font-family: Arial; z-index: 9999;">
            <b>Usos del suelo</b><br>{items_html}
        </div>
        """
        return Template(html)

    def _crear_mapa(self) -> folium.Map:
        """
        Construye el mapa Folium:

        - Base map centrado en Andalucía
        - Capa GeoJson con estilo por fclass
        - Tooltip con fclass, name y area_ha (hectáreas)
        - Ajuste automático de vista al bbox
        - Leyenda y control de capas
        """
        m = folium.Map(
            location=ANDALUCIA_CENTER,
            zoom_start=ANDALUCIA_ZOOM,
            control_scale=True,
        )

        # Si hay datos filtrados en memoria, los dibujamos.
        if self.geojson is not None and "features" in self.geojson and len(self.geojson["features"]) > 0:
            folium.GeoJson(
                self.geojson,
                name="Usos del suelo",
                style_function=self._style_by_fclass,
                tooltip=folium.GeoJsonTooltip(
                    fields=["fclass", "name", "area_ha"],
                    aliases=["Clase", "Nombre", "Superficie (ha)"],
                ),
            ).add_to(m)

            # Ajustar la vista para encajar los datos en pantalla.
            if self._bbox is not None and len(self._bbox) == 4:
                minx, miny, maxx, maxy = self._bbox
                m.fit_bounds([[miny, minx], [maxy, maxx]])

            # Añadir leyenda (HTML flotante)
            macro = MacroElement()
            macro._template = self._legend_control()
            m.get_root().add_child(macro)

        # Control para activar/desactivar capas si hubiera más en el futuro
        folium.LayerControl().add_to(m)
        return m

    # =============================================================================
    # 4) ACCIONES UI: visualizar y renderizar
    # =============================================================================
    def visualizar(self):
        """
        Acción asociada al botón "Visualizar":
        - Lee y filtra desde BD
        - Si hay datos, genera y renderiza el mapa
        """
        ok = self._leer_y_filtrar()
        if ok:
            self.render_mapa()

    def render_mapa(self):
        """
        Genera HTML temporal del mapa y lo carga en el visor web.

        Importante:
        - m.save(..., embed=True) incrusta recursos Leaflet y evita problemas con CDNs
          o acceso a recursos remotos desde contenido local.
        """
        try:
            m = self._crear_mapa()
            tmp = os.path.join(tempfile.gettempdir(), "mapa_andalucia.html")
            m.save(tmp, embed=True)
            self.view.setUrl(QUrl.fromLocalFile(tmp))
            self.log_msg("Mapa renderizado en visor.")
        except Exception as e:
            QMessageBox.critical(self, "Error al visualizar", str(e))
            self.log_msg(f"Error al visualizar: {e}")



def main():
    """
    Inicializa QApplication, crea la ventana principal y arranca el event loop.
    """
    app = QApplication(sys.argv)
    w = App()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
