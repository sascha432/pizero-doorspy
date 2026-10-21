#!/bin/bash
# Seed Frigate's face library for one person from existing event snapshots.
#
# Frigate 0.18 saves no "Recent Recognitions" while the library is empty (classify() returns
# None and process_frame exits before write_face_attempt), so the first faces must come from
# somewhere else. This crops the face from every clean event snapshot (YuNet, the same detector
# Frigate uses), stores the crops as training images for NAME and restarts Frigate to rebuild.
#
# Usage on acidpi4:  ./seed-faces.sh NAME ['GLOB']
#   GLOB defaults to all clean doorspy snapshots; narrow it to events that show only NAME, e.g.
#   ./seed-faces.sh sascha 'doorspy-17924478*-clean.webp'
# Review the result in Frigate > Face Library and delete wrong crops there.
set -euo pipefail

NAME=${1:?usage: seed-faces.sh NAME [GLOB]}
GLOB=${2:-doorspy-*-clean.webp}
cd "$(dirname "$0")"

docker exec -i -e NAME="$NAME" -e GLOB="$GLOB" frigate python3 - <<'EOF'
import cv2, glob, numpy as np, os
name, pattern = os.environ["NAME"], os.environ["GLOB"]
det = cv2.FaceDetectorYN.create("/config/model_cache/facedet/facedet.onnx", "", (320, 320), 0.5, 0.3)
out = f"/media/frigate/clips/faces/{name}"
os.makedirs(out, exist_ok=True)
saved = 0
for f in sorted(glob.glob(f"/media/frigate/clips/{pattern}")):
    img = cv2.imread(f)
    if img is None:
        continue
    det.setInputSize((img.shape[1], img.shape[0]))
    _, faces = det.detect(img)
    if faces is None:
        continue
    best = faces[np.argmax(faces[:, -1])]
    if best[-1] < 0.7:
        continue
    x, y, w, h = (int(v) for v in best[0:4])
    mx, my = w // 5, h // 5                       # 20% margin, like an upload crop
    crop = img[max(y - my, 0):min(y + h + my, img.shape[0]),
               max(x - mx, 0):min(x + w + mx, img.shape[1])]
    base = os.path.basename(f).replace("-clean.webp", "")
    cv2.imwrite(f"{out}/seed-{base}.webp", crop)
    saved += 1
    print(f"{base}: face {w}x{h} score {best[-1]:.2f}")
print(f"{saved} face(s) saved to {out}")
EOF

docker compose restart frigate
