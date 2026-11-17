import sys, time
from remotezip import RemoteZip

BASE = "https://opendata.geoportal.gov.pl/InneDane/Drzewa3D/LOD1/2023"

def open_zip(url):
    return RemoteZip(url, timeout=(30, 180))

def members(url):
    """sorted [(name, compressed_size)] of the .gml members (central directory only)."""
    for a in range(6):
        try:
            with open_zip(url) as z:
                m = [(i.filename, i.compress_size) for i in z.infolist()
                     if not i.is_dir() and i.filename.lower().endswith(".gml")]
            return sorted(m)
        except Exception as e:
            print(f"retry listing {url}: {e!r}", file=sys.stderr)
            time.sleep(10 * (a + 1))
    raise RuntimeError(f"cannot list {url}")