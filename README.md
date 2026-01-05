# Deprecation:

The version 2.0.0 of the generic snakemake plugin for slurm lets the user dynamically
define partitions according to resources. This plugin is now obsolete. Please use
the official snakemake plugin.

# Snakemake executor plugin: slurm-gustave-roussy

Snakemake plugin executor designed to match [Gustave Roussy](https://www.gustaveroussy.fr/en) computing cluster specificities : automatic partition seleciton, and default resources value.

Checkout the profile given on the cluster to automatically activate this executor with your pipeline:

`snakemake --profile /mnt/beegfs/pipelines/unofficial-snakemake-wrappers/profiles/slurm-web ... `

For documentation, see the [Snakemake plugin catalog](https://snakemake.github.io/snakemake-plugin-catalog/plugins/executor/slurm-gustave-roussy.html).
