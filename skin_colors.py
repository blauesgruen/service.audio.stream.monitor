"""
Liest Farbdefinitionen aus colors/Defaults.xml des aktiven Kodi-Skins
und aktualisiert das values-Attribut des bullet_color-Settings in der
bestehenden resources/settings.xml (in-place, Struktur bleibt erhalten).

Die settings.xml muss bereits vor dem Kodi-Start existieren und darf
niemals komplett neu geschrieben werden.
"""
import os
import xml.etree.ElementTree as ET

try:
    import xbmc
    import xbmcvfs
    _KODI = True
except ImportError:
    _KODI = False

# Fallback-Farben wenn kein Skin oder keine colors/Defaults.xml vorhanden
_FALLBACK_COLORS = [
    'white', 'grey', 'black', 'red', 'green', 'blue',
    'yellow', 'orange', 'selected', 'description',
]

# Pfad zur resources/settings.xml dieses Addons
_ADDON_ROOT   = os.path.dirname(os.path.abspath(__file__))
_SETTINGS_XML = os.path.join(_ADDON_ROOT, 'resources', 'settings.xml')


def _translate_path(special_path):
    if not _KODI:
        return special_path
    try:
        return xbmcvfs.translatePath(special_path)
    except Exception:
        return xbmc.translatePath(special_path)


def get_skin_colors():
    """
    Liest colors/Defaults.xml des aktiven Skins.
    Gibt ein dict {name: argb_value} zurück (Reihenfolge wie in der Datei).
    Bei Fehler oder fehlendem Skin wird ein leeres dict zurückgegeben.
    """
    if not _KODI:
        return {}
    skin_dir    = _translate_path('special://skin/')
    colors_file = os.path.join(skin_dir, 'colors', 'Defaults.xml')
    colors = {}
    try:
        tree = ET.parse(colors_file)
        for elem in tree.getroot().findall('color'):
            name  = (elem.get('name') or '').strip()
            value = (elem.text or '').strip()
            if name and value:
                colors[name] = value
    except FileNotFoundError:
        pass
    except ET.ParseError as exc:
        if _KODI:
            xbmc.log(
                f'[AudioStreamMonitor] skin_colors: XML-Fehler in {colors_file}: {exc}',
                xbmc.LOGWARNING,
            )
    except Exception as exc:
        if _KODI:
            xbmc.log(
                f'[AudioStreamMonitor] skin_colors: Fehler beim Lesen der Skinfarben: {exc}',
                xbmc.LOGWARNING,
            )
    return colors


def update_settings_colors():
    """
    Liest die Farben des aktiven Skins und aktualisiert das values-Attribut
    des <setting id="bullet_color">-Elements in der bestehenden settings.xml.
    Die Dateistruktur (alle anderen Settings) bleibt vollständig erhalten.
    Gibt die Liste der Farbnamen zurück.
    """
    colors      = get_skin_colors()
    color_names = list(colors) if colors else list(_FALLBACK_COLORS)
    values      = '|'.join(color_names)

    try:
        tree = ET.parse(_SETTINGS_XML)
        root = tree.getroot()
        for setting in root.iter('setting'):
            if setting.get('id') == 'bullet_color':
                setting.set('values', values)
                break
        tree.write(_SETTINGS_XML, encoding='utf-8', xml_declaration=True)
        if _KODI:
            xbmc.log(
                f'[AudioStreamMonitor] skin_colors: {len(color_names)} Skinfarben in settings.xml eingetragen',
                xbmc.LOGINFO,
            )
    except Exception as exc:
        if _KODI:
            xbmc.log(
                f'[AudioStreamMonitor] skin_colors: settings.xml konnte nicht aktualisiert werden: {exc}',
                xbmc.LOGWARNING,
            )
    return color_names
