## Highlights

- The bootstrap installers no longer use administrator rights or install
  OS packages. Install `curl` and GitHub CLI 2.95 or newer first.
- Linux installation works on x64 distributions based on glibc, including
  Ubuntu 26.04, and accepts home directories writable only by the user's
  private group.
- Downloads are staged in the private Site Ops data folder, so temporary
  directories with inherited access rules no longer block installation.
- Site Ops checks the GitHub CLI executable before verifying sources.
- This is a qualification prerelease for the maintainer fork.
