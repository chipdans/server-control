# Updates an existing v2 hub. Does not recreate databases, users or secrets.
$ErrorActionPreference = "Stop"
Push-Location $PSScriptRoot
$DeployConfig = Join-Path $PSScriptRoot ("wrangler.quota-" + [guid]::NewGuid().ToString("N") + ".toml")
try {
    & npm.cmd ci
    if ($LASTEXITCODE -ne 0) { throw "npm ci failed; deployment stopped." }

    $DatabaseJson = & npx.cmd wrangler d1 list --json
    if ($LASTEXITCODE -ne 0) { throw "Cloudflare access failed. Run npx.cmd wrangler login and retry." }
    $Databases = @(($DatabaseJson | ConvertFrom-Json) | Where-Object { $_.name -eq "server-control" })
    if ($Databases.Count -ne 1) { throw "Expected exactly one existing D1 database named server-control." }
    $DatabaseId = if ($Databases[0].uuid) { [string]$Databases[0].uuid } else { [string]$Databases[0].id }
    if ($DatabaseId -notmatch '^[0-9a-fA-F-]{36}$') { throw "Invalid D1 database ID." }
    $Config = (Get-Content (Join-Path $PSScriptRoot "wrangler.toml") -Raw).Replace("REPLACE_WITH_D1_DATABASE_ID", $DatabaseId)
    # Keep the config beside src/ so Wrangler resolves relative paths correctly.
    [IO.File]::WriteAllText($DeployConfig, $Config, [Text.UTF8Encoding]::new($false))

    # Both migrations contain only CREATE INDEX IF NOT EXISTS. Earlier v2
    # installations may have applied schema migrations without tracking them.
    foreach ($Migration in @("0005_sync_indexes.sql", "0006_d1_quota_indexes.sql")) {
        & npx.cmd wrangler d1 execute server-control --remote --config $DeployConfig --file (Join-Path $PSScriptRoot "migrations/$Migration") --yes
        if ($LASTEXITCODE -ne 0) {
            throw "Index migration failed. If the daily D1 quota is exhausted, retry after 00:00 UTC. Worker was not deployed."
        }
    }

    & npx.cmd wrangler deploy --config $DeployConfig --keep-vars
    if ($LASTEXITCODE -ne 0) { throw "Worker deployment failed. Do not treat this run as successful." }
    Write-Host "RESULT=SUCCESS"
    Write-Host "D1_INDEXES=APPLIED"
    Write-Host "WORKER=DEPLOYED"
    Write-Host "This does not reset an already exhausted daily quota. Test login after quota recovery."
}
finally {
    # Only the generated local config is removed; the checkout and data remain.
    if (Test-Path -LiteralPath $DeployConfig) { Remove-Item -LiteralPath $DeployConfig }
    Pop-Location
}
