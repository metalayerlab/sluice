# Quick Start

From nothing to a working sluice in a few minutes. Everything else is in [In-Depth Configuration](In-Depth-Configuration).

## Prerequisites

- For running from source: Python >= 3.13 and [uv](https://docs.astral.sh/uv/)
- Linux with FUSE 3: `/dev/fuse`, and `fusermount3` (package `fuse3`)
- To build pyfuse3, which ships only as source: libfuse3 headers, `pkg-config` and a C compiler. On Debian or Ubuntu: `sudo apt install libfuse3-dev pkg-config gcc`. On Arch or Manjaro: `sudo pacman -S fuse3 pkgconf gcc`.

sluice talks to nothing over the network. The only things it touches are the directories your routes point at.

## Install

```sh
uv tool install 'sluice[fuse] @ git+https://github.com/metalayerlab/sluice'
```

Without the `[fuse]` extra, `sluice routes` and `sluice resolve` work but `sluice mount` exits with `pyfuse3 is not installed`. To run from a checkout, use `uv sync --extra fuse`.

## First run

1. Make a backing directory with a file in it, and an empty mount point:

   ```sh
   mkdir -p /tmp/sluice-demo/data/alice /tmp/sluice-demo/mnt
   echo hello > /tmp/sluice-demo/data/alice/notes.txt
   ```

2. Write the app, `/tmp/sluice-demo/demo.py`:

   ```python
   from sluice import Sluice, VirtualHandler, access_log, auth


   async def verify(user, token, real_path, op):
       return token == "T0k"


   class Status(VirtualHandler):
       async def render(self, ctx):
           return f"hello {ctx.params['user']}\n".encode()


   app = Sluice()
   app.use(access_log())
   app.param("token", secret=True)
   user = app.route(
       "/home/{user}/{token}", backend="/tmp/sluice-demo/data/{user}", middlewares=[auth(verify)]
   )
   user.route("status", virtual=True, handler=Status())
   ```

3. Point sluice at it and check the routes without mounting:

   ```console
   $ export SLUICE_APP_DIR=/tmp/sluice-demo SLUICE_APP_MODULE=demo:app
   $ sluice routes
   /home/{user}/{token}  [backend /tmp/sluice-demo/data/{user}]
     middleware: access_log(), auth(verify)
     /home/{user}/{token}/status  [virtual]
       middleware: access_log(), auth(verify)
   ```

4. Mount it in one terminal (it stays in the foreground and logs every call):

   ```sh
   SLUICE_MOUNT_POINT=/tmp/sluice-demo/mnt sluice mount
   ```

5. Use it from another terminal:

   ```console
   $ ls /tmp/sluice-demo/mnt/home/alice/T0k
   notes.txt  status
   $ cat /tmp/sluice-demo/mnt/home/alice/T0k/status
   hello alice
   $ cat /tmp/sluice-demo/mnt/home/alice/wrong/notes.txt
   cat: /tmp/sluice-demo/mnt/home/alice/wrong/notes.txt: Permission denied
   ```

6. Stop it with Ctrl-C in the first terminal: it unmounts and exits 0.

## Next steps

- [Writing an App](Writing-an-App): routes, middleware, virtual files
- [In-Depth Configuration](In-Depth-Configuration): every setting
- [Deployment](Deployment): running it in a container
- [Troubleshooting](Troubleshooting) if something doesn't work
