<#
.SYNOPSIS
  Retired guard for the old second-pass PAD encoder.

.DESCRIPTION
  Workflow Version 4 repairs confirmed PADs in the source-derived AVS before the
  sole final x265 encode. This compatibility stub intentionally performs no I/O.
  Run encode_episode.ps1 -Pass final after building avs_padfix instead.
#>
param([Parameter(Mandatory)][string]$Episode)

throw "encode_padfixed.ps1 is retired in Workflow Version 4. Build the source-derived avs_padfix and run encode_episode.ps1 -Episode $Episode -Pass final exactly once."
