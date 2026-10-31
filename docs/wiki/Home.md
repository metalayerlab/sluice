# sluice

Sluice — a Python FUSE filesystem with an Express-style router that gates, rewrites, and middleware-intercepts every filesystem call between virtual paths and their backing stores.

You write a small Python app that maps virtual paths to real directories (or to content generated on the fly), and attach async middleware that sees every filesystem call: to allow or deny it, log it, or rewrite what comes back. `sluice mount` serves that app as a filesystem. It is a lab project for people who want per-call control over a filesystem in Python, such as multi-tenant views of one disk, virtual status files, or redacted reads, without writing a FUSE driver.

The smallest example: one route, with the user's name and a token in the path.

```python
# myroutes.py
from sluice import Sluice, access_log, auth


async def verify(user, token, real_path, op):
    return token == "T0k"  # your own check goes here


app = Sluice()
app.use(access_log())
app.param("token", secret=True)  # masked in logs
app.route("/home/{user}/{token}", backend="/srv/data/{user}", middlewares=[auth(verify)])
```

```console
$ SLUICE_APP_MODULE=myroutes:app SLUICE_MOUNT_POINT=/mnt/sluice sluice mount &
$ cat /mnt/sluice/home/alice/T0k/notes.txt      # served from /srv/data/alice/notes.txt
hello
$ cat /mnt/sluice/home/alice/wrong/notes.txt
cat: /mnt/sluice/home/alice/wrong/notes.txt: Permission denied
```

The access log shows each call, with the token masked:

```text
INFO sluice.access: read /home/alice/********/notes.txt uid=1000 pid=4077157 -> ok (0.1ms) [12]
INFO sluice.access: lookup /home/alice/******** uid=1000 pid=4077170 -> EACCES (0.0ms) [39]
```

## Table of Contents

**Getting started**: [Introduction](Introduction) · [Quick Start](Quick-Start)

**Using it**: [Writing an App](Writing-an-App) · [In-Depth Configuration](In-Depth-Configuration) · [CLI Reference](CLI-Reference) · [Deployment](Deployment)
**Help**: [Troubleshooting](Troubleshooting) · [FAQ](Frequently-Asked-Questions) · [Security](Security)

**Developers**: [Contributing](Contributing) · [Architecture Internals](Architecture-Internals) · [Testing Guide](Testing-Guide) · [Release Process](Release-Process)
