import sys, zipfile, gzip, json

zip_path, out_path = sys.argv[1], sys.argv[2]

def extract_info(name, fileobj):
    # TODO: parse the member and return a dict of what you need
    size = 0
    while chunk := fileobj.read(1 << 20):
        size += len(chunk)
    return {"file": name, "bytes": size}

with zipfile.ZipFile(zip_path) as z, gzip.open(out_path, "wt") as out:
    for info in z.infolist():
        if info.is_dir():
            continue
        with z.open(info) as f:
            out.write(json.dumps(extract_info(info.filename, f)) + "\n")