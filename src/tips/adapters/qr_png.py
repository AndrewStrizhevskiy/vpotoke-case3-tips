"""PNG с QR-кодом через qrencode, если он установлен."""
import os
import shutil
import subprocess


class QrencodeRenderer:
    def __init__(self, png_dir):
        self.png_dir = png_dir

    def render(self, url, qr_id):
        if not shutil.which("qrencode"):
            return None
        os.makedirs(self.png_dir, exist_ok=True)
        path = os.path.join(self.png_dir, qr_id + ".png")
        done = subprocess.run(["qrencode", "-o", path, "-s", "8", url], capture_output=True)
        return path if done.returncode == 0 else None
