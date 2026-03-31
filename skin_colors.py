"""
Liest Farbdefinitionen aus colors/Defaults.xml des aktiven Kodi-Skins
und schreibt resources/settings.xml mit dynamischer Farbauswahl.

Das Dropdown in den Addon-Settings wird beim Service-Start mit den
tatsächlichen Farbnamen des gerade aktiven Skins befüllt.
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


def _build_settings_xml(color_names, default_color):
    """Erzeugt den Inhalt der resources/settings.xml mit den gegebenen Farboptionen."""
    options_lines = '\n'.join(
        f'            <option label="{name}">{name}</option>'
        for name in color_names
    )
    return (
        '<?xml version="1.0" encoding="utf-8" standalone="yes"?>\n'
        '<settings version="1.1">\n'
        '  <category id="display" label="32000">\n'
        '    <group id="1" label="32010">\n'
        '      <setting id="bullet_enabled" type="bool" label="32001">\n'
        '        <level>0</level>\n'
        '        <default>true</default>\n'
        '      </setting>\n'
        '      <setting id="bullet_color" type="select" label="32002">\n'
        '        <level>0</level>\n'
        f'        <default>{default_color}</default>\n'
        '        <constraints>\n'
        '          <options>\n'
        f'{options_lines}\n'
        '          </options>\n'
        '        </constraints>\n'
        '        <dependencies>\n'
        '          <enable operator="eq" setting="bullet_enabled">true</enable>\n'
        '        </dependencies>\n'
        '      </setting>\n'
        '    </group>\n'
        '  </category>\n'
        '</settings>\n'
    )


def update_settings_colors():
    """
    Liest die Farben des aktiven Skins und ersetzt das values-Attribut
    im <setting id="bullet_color">-Element in settings.xml (labelenum/values-Variante).
    Setzt das Label von bullet_preview dynamisch auf 'Aktuelle Farbe: <name>'.
    """
    colors = get_skin_colors()
    color_names = list(colors) if colors else list(_FALLBACK_COLORS)
    default_color = 'green' if 'green' in color_names else color_names[0]

    bullet_values = [f' {name}' for name in color_names]
    default_bullet = f' {default_color}'

    try:
        tree = ET.parse(_SETTINGS_XML)
        root = tree.getroot()
        current_color = default_color
        # Finde das <setting id="bullet_color"> und <setting id="bullet_preview">
        for setting in root.iter('setting'):
            if setting.get('id') == 'bullet_color':
                setting.set('values', '|'.join(bullet_values))
                # Ermittle aktuelle Auswahl (ohne Bullet)
                sel = setting.get('default', default_bullet)
                if sel.startswith(' '):
                    current_color = sel[2:].strip()
                else:
                    current_color = sel.strip()
                setting.set('default', f' {current_color}')
            if setting.get('id') == 'bullet_preview':
                # Setze das Label auf Vorschau mit Punkt und aktuellem Farbnamen
                setting.set('label', f' Aktuelle Farbe: {current_color}')
        tree.write(_SETTINGS_XML, encoding='utf-8', xml_declaration=True)
        if _KODI:
            xbmc.log(
                f'[AudioStreamMonitor] skin_colors: {len(color_names)} Farben aus Skin geladen – Standard: "{current_color}"',
                xbmc.LOGINFO,
            )
    except Exception as exc:
        if _KODI:
            xbmc.log(
                f'[AudioStreamMonitor] skin_colors: settings.xml konnte nicht aktualisiert werden: {exc}',
                xbmc.LOGWARNING,
            )
    return color_names
