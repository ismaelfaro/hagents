"""Let nanoLoop's file tools accept absolute paths that are already inside
HARNESS_WORKDIR. Upstream re-roots every absolute path under the workdir, so an
agent that runs `pwd`, sees /node, and writes /node/STATUS.md ends up writing
/node/node/STATUS.md. Remove this patch once nanoLoop handles it upstream.
"""
import pathlib

import nanoloop.tools as tools

src = pathlib.Path(tools.__file__)
text = src.read_text()
old = '''    p = Path(path)
    # Strip leading slash / drive so absolute paths land inside the workspace.
'''
new = '''    p = Path(path)
    # hagents patch: an absolute path already inside WORKDIR is taken as is.
    if p.is_absolute() and p.resolve().is_relative_to(WORKDIR):
        return p.resolve()
    # Strip leading slash / drive so absolute paths land inside the workspace.
'''
if new not in text:
    assert old in text, "nanoLoop's _resolve changed; update docker/nanoloop/patch_resolve.py"
    src.write_text(text.replace(old, new, 1))
print("patched", src)
