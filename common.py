import sys, time, zipfile
from remotezip import RemoteZip

BASE = "https://opendata.geoportal.gov.pl/InneDane/Drzewa3D/LOD1/2023"


class BadArchive(Exception):
    """Permanent failure: the remote file is not a readable zip (e.g. truncated,
    no end-of-central-directory record). Retrying cannot help."""


def open_zip(url):
    return RemoteZip(url, timeout=(30, 180))


def members(url):
    """sorted [(name, compressed_size)] of the .gml members (central directory only).

    Raises BadArchive for a corrupt/truncated zip (no retry),
    RuntimeError if listing keeps failing for transient reasons.
    """
    for a in range(6):
        try:
            with open_zip(url) as z:
                m = [(i.filename, i.compress_size) for i in z.infolist()
                     if not i.is_dir() and i.filename.lower().endswith(".gml")]
            return sorted(m)
        except zipfile.BadZipFile as e:
            # deterministic: do not retry
            raise BadArchive(f"{url}: {e}") from e
        except Exception as e:
            print(f"retry listing {url}: {e!r}", file=sys.stderr)
            time.sleep(10 * (a + 1))
    raise RuntimeError(f"cannot list {url}")