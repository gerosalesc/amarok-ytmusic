# AUR Packaging (`amarok-ytmusic`)

## Publishing to the Arch User Repository

1. Ensure your SSH key is registered on [aur.archlinux.org](https://aur.archlinux.org).
2. Clone the AUR package repository (first time creates it):
   ```bash
   git clone ssh://aur@aur.archlinux.org/amarok-ytmusic.git /tmp/aur-amarok-ytmusic
   ```
3. Copy the files from this directory into `/tmp/aur-amarok-ytmusic`:
   ```bash
   cp packaging/aur/PKGBUILD packaging/aur/amarok-ytmusic.install packaging/aur/amarok-ytmusic.service /tmp/aur-amarok-ytmusic/
   ```
4. Pin the release tarball SHA256 and regenerate `.SRCINFO` (on Arch Linux with `pacman-contrib`):
   ```bash
   cd /tmp/aur-amarok-ytmusic
   updpkgsums
   makepkg --printsrcinfo > .SRCINFO
   ```
   *(If you're not on Arch Linux, you can compute the tarball SHA256 with `curl -fsSL https://github.com/gerosalesc/amarok-ytmusic/archive/refs/tags/v0.1.1.tar.gz | sha256sum`, replace `'SKIP'` in `PKGBUILD` and `.SRCINFO`, and commit.)*
5. Commit and push to AUR:
   ```bash
   git add PKGBUILD .SRCINFO amarok-ytmusic.install amarok-ytmusic.service
   git commit -m "Release amarok-ytmusic 0.1.1"
   git push origin master
   ```
