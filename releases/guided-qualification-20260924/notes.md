## Highlights

Install an identified Site Ops build from this maintainer fork and acquire
the compatible IoT Operations workspace without cloning the repository.
The PowerShell and Bash bootstrap scripts check the configured Python
package feed before provisioning tools and require private storage before
using retained executables. The HTTPS and independently verified script
routes have distinct initial trust, and both verify the engine bundle
before installation.

Inspect typed `aio-install` inputs, select one existing Arc-connected
cluster through manual answers or an explicitly authorized resource read,
then review the Site and plan before deployment. Secret Sync can be enabled
in the same AIO deployment after the cluster's OIDC and workload identity
prerequisites pass. Configure a separate set of new Sites and an explicit
name selector for a later fleet deployment.

## Upgrading

Select this release's exact tag and source commit when installing a new
build. If a different Site Ops build is already installed, review the
documented `-Replace` or `--replace` option before changing the pipx
installation. Source enrollment and Azure authentication remain separate
operator decisions.
