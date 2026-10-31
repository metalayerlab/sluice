# Deployment

sluice is one foreground process per mount. It needs the FUSE device, permission to mount, your app's module, and the directories its routes point at. It opens no ports.

## What a mount needs

- **`/dev/fuse`**, from the `fuse` kernel module (`modprobe fuse`).
- **Permission to mount.** As a normal user, through `fusermount3`, which the `fuse3` package installs setuid root. As root, or with `CAP_SYS_ADMIN`, libfuse mounts directly.
- **libfuse3**, and pyfuse3 built against it: the `sluice[fuse]` extra. The container image has both.
- **`user_allow_other` in `/etc/fuse.conf`**, only if you set `mount.allow_other` and don't run as root.
- **An empty mount point** that sluice's user can write to, and read and write access to every backing directory.

Stop it with SIGTERM or SIGINT: it unmounts and exits 0. If it was killed, unmount by hand with `fusermount3 -u <mount.point>` before starting it again.

## systemd

```ini
# /etc/systemd/system/sluice.service
[Unit]
Description=sluice FUSE mount
After=local-fs.target

[Service]
User=sluice
Environment=SLUICE_CONFIG=/etc/sluice/config.yaml
LoadCredential=db_password:/etc/sluice/secrets/db_password
Environment=SLUICE_SECRETS_DIR=%d
ExecStart=/usr/local/bin/sluice mount
ExecStopPost=-/usr/bin/fusermount3 -u /mnt/sluice
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

`ExecStopPost` cleans up after a crash, so the restart doesn't find a stale mount. Without `allow_other`, only the `sluice` user can enter the mount.

## Container

<!-- TODO(wiki): the container recipe below hasn't been run end to end (image build and a mount inside Docker); verify it, then remove this marker. -->

The image (`ghcr.io/metalayerlab/sluice`) runs `sluice` as uid 1000 and contains `fusermount3`. A FUSE mount inside a container needs more than most containers get:

```sh
docker run --rm \
  --device /dev/fuse \
  --cap-add SYS_ADMIN \
  --security-opt apparmor=unconfined \
  -e SLUICE_APP_DIR=/app -e SLUICE_APP_MODULE=routes:app \
  -e SLUICE_MOUNT_POINT=/mnt/sluice \
  -v "$PWD/app:/app:ro" \
  -v /srv/data:/srv/data \
  -v /mnt/sluice:/mnt/sluice:rshared \
  -v "$PWD/secrets:/run/secrets:ro" \
  ghcr.io/metalayerlab/sluice mount
```

- `--device /dev/fuse`: the FUSE device.
- `--cap-add SYS_ADMIN` and `apparmor=unconfined`: Docker's default AppArmor profile and capability set forbid `mount`. Grant nothing else: keep the default seccomp profile, a read-only root filesystem, and every other capability dropped.
- `:rshared` on the mount point's bind mount propagates the FUSE mount back to the host. Without it, the mount is visible only inside the container.
- The backing directories are bind-mounted at the paths your backend templates use. Secrets go to `/run/secrets`, the default `secrets.dir`.

**Kubernetes:** the same needs, as a FUSE device plugin (which hands out `/dev/fuse` without `privileged`) or a privileged container, `SYS_ADMIN`, an unconfined AppArmor profile, and `mountPropagation: Bidirectional` on the volume that holds the mount point if other containers or the node should see it. Put the app in a ConfigMap (mounted at `app.dir`) and secrets in a Secret volume at `/run/secrets`.

## Building the image

pyfuse3 ships only as source, so the builder stage installs `gcc`, `libc6-dev`, `libfuse3-dev` and `pkg-config` and syncs with `--extra fuse`. The runtime stage installs `fuse3`. Both are a workaround in the template-owned Dockerfile until the template has a system-packages hook ([project-template#53](https://github.com/metalayerlab/project-template/issues/53)).
