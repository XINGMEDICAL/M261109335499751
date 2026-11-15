#requires -Version 5.1
# Pruebas locales con datos ficticios. No consulta SQL/Odoo ni pide credenciales.
$ErrorActionPreference = 'Stop'
$client = Join-Path $PSScriptRoot 'Sync-WorldOffice.ps1'
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($client,[ref]$tokens,[ref]$parseErrors)
if ($parseErrors.Count -gt 0) { $parseErrors | Format-List; throw 'Hay errores de sintaxis en el cliente.' }
foreach ($name in @('Get-ValidatedSource','ConvertTo-OdooRecords')) {
    $functions = @($ast.FindAll({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst]},$true) | Where-Object { $_.Name -eq $name })
    if ($functions.Count -ne 1) { throw "Funcion no encontrada: $name" }
    . ([scriptblock]::Create($functions[0].Extent.Text))
}
function Assert-Test { param([bool]$Condition,[string]$Name) if (!$Condition) { throw "FALLO: $Name" }; Write-Host "OK: $Name" }
$script:sourceRejected = 0
$rows = @(@{source_id='1';code='P01';name='Ficticio'},@{source_id='2';code='P02';name='Ficticio'})
$valid = @(Get-ValidatedSource 'product' $rows)
Assert-Test ($valid.Count -eq 2 -and $script:sourceRejected -eq 0) 'dos productos validos'
$rows[1].code = 'P01'
$script:sourceRejected = 0
$valid = @(Get-ValidatedSource 'product' $rows)
Assert-Test ($valid.Count -eq 0 -and $script:sourceRejected -eq 2) 'se rechazan AMBOS codigos duplicados'
$script:sourceRejected = 0
$valid = @(Get-ValidatedSource 'employee' @(@{source_id='1';name='Ficticio';identification='001234'}))
Assert-Test ($valid.Count -eq 1 -and $valid[0]['identification'] -ceq '001234') 'documento con ceros intacto'
$script:sourceRejected = 0
$valid = @(Get-ValidatedSource 'employee' @(@{source_id='1';name=' ';identification='001234'}))
Assert-Test ($valid.Count -eq 0 -and $script:sourceRejected -eq 1) 'no se importa nombre vacio'
$script:sourceRejected = 0
$valid = @(Get-ValidatedSource 'asset' @(@{source_id='1';name='Equipo';code='AF1';serial=''},@{source_id='2';name='Equipo';code='AF2';serial=''}))
Assert-Test ($valid.Count -eq 2) 'serial vacio no es duplicado'
$script:sourceRejected = 0
$valid = @(Get-ValidatedSource 'asset' @(@{source_id='1';name='Equipo';code='AF1';serial='S1'},@{source_id='2';name='Equipo';code='AF2';serial='S1'}))
Assert-Test ($valid.Count -eq 0 -and $script:sourceRejected -eq 2) 'serial duplicado bloqueado'
$fake = @(@([pscustomobject]@{id=1},[pscustomobject]@{id=2}))
$records = @(ConvertTo-OdooRecords $fake)
Assert-Test ($records.Count -eq 2 -and $records[0].id -eq 1) 'respuesta API normalizada'
$empty = @(ConvertTo-OdooRecords @())
Assert-Test ($empty.Count -eq 0) 'lista API vacia no cuenta como producto'
$batch = New-Object System.Collections.ArrayList
[void]$batch.Add(@{source_id='1';name='Ejemplo';identification='001'})
$data = @{rows=@($batch.ToArray());dry_run=$true;company_id=1}
$decoded = ($data | ConvertTo-Json -Depth 30 -Compress) | ConvertFrom-Json
Assert-Test ($decoded.rows.Count -eq 1 -and $decoded.rows[0].identification -eq '001' -and $decoded.dry_run -is [bool]) 'lote JSON conserva lista y booleano'
Write-Host 'PRUEBAS CLIENTE OK. No se consultaron servicios ni credenciales.' -ForegroundColor Green
