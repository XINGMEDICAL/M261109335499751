#requires -Version 5.1
# Catalogo completo, empleados y activos fijos desde vistas confirmadas de World Office.
# Simulacion por defecto. Requiere instalar xing_wo_sync en Odoo.
# No guarda credenciales ni usa la SQLite de la demo.
[CmdletBinding()]
param(
    [string]$OdooUrl = 'https://xingmedical-m261109335499751.odoo.com',
    [string]$WoServer = 'APP_XING314\WORLDOFFICE',
    [string]$WoDatabase = '2026 Xingmedical SAS',
    [string]$WoUser = 'wo_cliente',
    [switch]$UseClipboard,
    [switch]$SqlPasswordFromClipboard,
    [ValidateSet('ProductsEmployees','Products','Employees','Assets','All')][string]$Scope = 'All',
    [int]$CompanyId = 1,
    [ValidateRange(1,50)][int]$BatchSize = 50,
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$target = [Uri]$OdooUrl
if ($target.Scheme -ne 'https' -or $target.UserInfo -or $target.Query -or $target.Fragment -or $target.AbsolutePath -ne '/') {
    throw 'Indique solo el servidor HTTPS, sin /odoo ni credenciales.'
}
$OdooUrl = $OdooUrl.TrimEnd('/')
$headers = $null
$selectedCompanyId = $CompanyId
$apiKey = $null
$secureKey = $null
$sqlPassword = $null
$sqlPasswordText = $null
$pointer = [IntPtr]::Zero
$conn = $null
$cmd = $null
$adapter = $null
$table = $null

function Get-ConnectionFailure {
    param($Failure)
    $status = 'sin respuesta HTTP'
    $detail = [string]$Failure.Exception.Message
    $exception = $Failure.Exception
    while ($null -ne $exception) {
        if ($null -ne $exception.PSObject.Properties['Response'] -and $null -ne $exception.Response) {
            $status = [string][int]$exception.Response.StatusCode
        }
        if ($exception -is [System.Net.WebException]) {
            $detail = "Red=$($exception.Status); $detail"
        }
        $exception = $exception.InnerException
    }
    if ($apiKey) { $detail = $detail.Replace($apiKey,'[REDACTADO]') }
    return "HTTP=$status | $detail"
}

function ConvertTo-OdooRecords {
    param($Value)
    if ($null -eq $Value) { return }
    $queue = New-Object System.Collections.Queue
    $queue.Enqueue($Value)
    while ($queue.Count -gt 0) {
        $item = $queue.Dequeue()
        if ($null -eq $item) { continue }
        if ($item -is [System.Collections.IList]) {
            foreach ($child in $item) { $queue.Enqueue($child) }
            continue
        }
        $recordId = 0
        if ($item.PSObject.Properties.Name -notcontains 'id' -or
            -not [int]::TryParse([string]$item.id, [ref]$recordId) -or $recordId -le 0) {
            throw 'Respuesta API inesperada: se esperaba un registro con ID. Comparacion detenida.'
        }
        Write-Output -NoEnumerate $item
    }
}

function Invoke-Connector {
    param([ValidateSet('connector_info','sync_batch')][string]$Method,[hashtable]$Data)
    $json = $Data | ConvertTo-Json -Depth 30 -Compress
    try {
        Invoke-RestMethod -Method Post -Uri "$OdooUrl/json/2/xing.wo.binding/$Method" `
            -Headers $headers -ContentType 'application/json; charset=utf-8' `
            -Body ([Text.Encoding]::UTF8.GetBytes($json)) -TimeoutSec 180 -MaximumRedirection 0
    } catch {
        $diagnostic = Get-ConnectionFailure $_
        throw "Fallo del conector ($Method). Revise que xing_wo_sync este instalado y el usuario tenga acceso. No hay reintento automatico; puede repetir la ejecucion con la misma fuente para reconciliar lotes confirmados. $diagnostic"
    }
}

function Read-SourceRows {
    param([string]$Query)
    $sourceCmd = $conn.CreateCommand()
    $sourceCmd.CommandText = $Query
    $sourceCmd.CommandTimeout = 120
    $sourceAdapter = New-Object System.Data.SqlClient.SqlDataAdapter -ArgumentList $sourceCmd
    $sourceTable = New-Object System.Data.DataTable
    try {
        [void]$sourceAdapter.Fill($sourceTable)
        foreach ($row in $sourceTable.Rows) {
            $item = @{}
            foreach ($column in $sourceTable.Columns) {
                if ($row[$column.ColumnName] -eq [DBNull]::Value) { $item[$column.ColumnName] = '' }
                else { $item[$column.ColumnName] = ([string]$row[$column.ColumnName]).Trim() }
            }
            Write-Output -NoEnumerate $item
        }
    } finally {
        $sourceAdapter.Dispose(); $sourceCmd.Dispose(); $sourceTable.Dispose()
    }
}

function Get-ValidatedSource {
    param([string]$Kind,[object[]]$Rows)
    $natural = if ($Kind -eq 'employee') { 'identification' } else { 'code' }
    $fields = @('source_id',$natural)
    if ($Kind -eq 'asset') { $fields += 'serial' }
    $counts = @{}
    foreach ($field in $fields) { $counts[$field] = @{} }
    foreach ($row in $Rows) {
        foreach ($field in $fields) {
            $key = [string]$row[$field]
            if ($key) {
                if (!$counts[$field].ContainsKey($key)) { $counts[$field][$key] = 0 }
                $counts[$field][$key]++
            }
        }
    }
    foreach ($row in $Rows) {
        $reason = ''
        if (!$row['source_id'] -or [string]$row['source_id'] -notmatch '^[a-zA-Z0-9_.:-]{1,100}$') { $reason = 'invalid_source_id' }
        elseif ([string]::IsNullOrWhiteSpace([string]$row['name'])) { $reason = 'missing_name' }
        elseif ([string]::IsNullOrWhiteSpace([string]$row[$natural])) { $reason = 'missing_identity' }
        else {
            foreach ($field in $fields) {
                $key = [string]$row[$field]
                if ($key -and $counts[$field][$key] -gt 1) { $reason = "duplicate_$field" }
            }
        }
        if ($reason) {
            Write-Host "OMITIDO | tipo=$Kind | WO_ID=$($row['source_id']) | motivo=$reason"
            $script:sourceRejected++
        } else { Write-Output -NoEnumerate $row }
    }
}

try {
    Write-Host ("WORLD OFFICE -> ODOO | Alcance=$Scope | Modo={0}" -f $(if($Apply){'APLICAR'}else{'SIMULACION'}))
    if ($CompanyId -le 0) { throw 'Indique una empresa valida con -CompanyId.' }
    Write-Host "Odoo: $OdooUrl"
    Write-Host "World Office: $WoServer | $WoDatabase | usuario: $WoUser"
    Write-Host 'Alcance segun -Scope: catalogo validado; empleados; activos del grupo ACTIVOS FIJOS.'
    Write-Host 'No incluye precios, existencias ni contenido comercial.'

    if ($UseClipboard) {
        # Copiar la clave ANTES de ejecutar; no copiar luego el comando.
        $apiKey = [string](Get-Clipboard -Raw)
    } else {
        $secureKey = Read-Host 'Clave API de ESTE Odoo.sh (oculta)' -AsSecureString
        $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
        $apiKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    }
    $apiKey = $apiKey.Trim()
    if ([string]::IsNullOrWhiteSpace($apiKey) -or $apiKey -match '\s|[\x00-\x1F\x7F]') {
        throw 'La clave recibida esta vacia o tiene caracteres adicionales. No se envio a Odoo.'
    }
    # Comprueba que el entorno sigue disponible antes de transmitir la clave.
    try {
        $version = Invoke-RestMethod -Method Get -Uri "$OdooUrl/web/version" -TimeoutSec 30 -MaximumRedirection 0
    } catch {
        $diagnostic = Get-ConnectionFailure $_
        throw "El destino no responde a /web/version. No se envio la clave a Odoo. Abra CONNECT en Odoo.sh y confirme la URL. $diagnostic"
    }
    if (-not $version.version_info -or [int]$version.version_info[0] -lt 19) {
        throw 'No se pudo confirmar una version compatible. No se envio la clave a Odoo.'
    }
    Write-Host "Destino disponible | Version: $($version.version)"
    $headers = @{ Authorization = "bearer $apiKey"; 'User-Agent' = 'XINGMEDICAL-Catalog-Preview' }

    Write-Host 'Comprobando modulo de sincronizacion ...'
    $info = Invoke-Connector 'connector_info' @{}
    if ([int]$info.protocol -ne 1 -or [int]$info.max_batch -lt $BatchSize) { throw 'Version del modulo incompatible.' }
    $company = @($info.companies | Where-Object { [int]$_.id -eq $CompanyId })
    if ($company.Count -ne 1) { throw 'La empresa no esta disponible para el usuario API.' }
    Write-Host "Destino: empresa ID=$CompanyId | $($company[0].name)"
    Write-Host 'No se importan precios, existencias, nomina, datos bancarios ni bajas automaticas.'

    if ($SqlPasswordFromClipboard) {
        Write-Host "Ahora copie SOLO la contrasena SQL de $WoUser al portapapeles de Windows."
        [void](Read-Host 'Cuando este copiada, pulse ENTER aqui SIN pegar la contrasena')
        $sqlPasswordText = [string](Get-Clipboard -Raw)
        # Las contrasenas SQL pueden contener espacios: no recortarlos.
        if ([string]::IsNullOrEmpty($sqlPasswordText)) { throw 'El portapapeles SQL esta vacio.' }
        if ($sqlPasswordText -match '[\x00-\x1F\x7F]') {
            throw 'La entrada SQL contiene saltos de linea o caracteres de control. Copie solo el valor; no se intento iniciar sesion.'
        }
        $sqlPassword = ConvertTo-SecureString -String $sqlPasswordText -AsPlainText -Force
        $sqlPasswordText = $null
        Write-Host 'Contrasena SQL leida del portapapeles (contenido oculto).'
    } else {
        $sqlPassword = Read-Host "Contrasena SQL Server para $WoUser (NO la clave de Odoo; entrada oculta)" -AsSecureString
    }
    if ($sqlPassword.Length -eq 0) { throw 'La contrasena SQL esta vacia.' }
    $sqlPassword.MakeReadOnly()
    $credential = New-Object System.Data.SqlClient.SqlCredential -ArgumentList $WoUser,$sqlPassword
    $csb = New-Object System.Data.SqlClient.SqlConnectionStringBuilder
    $csb['Data Source'] = $WoServer
    $csb['Initial Catalog'] = $WoDatabase
    $csb['Connect Timeout'] = 15
    $csb['Application Name'] = 'XINGMEDICAL Catalog Preview'
    # Conserva la configuracion TLS del conector anterior para el servidor local.
    $csb['TrustServerCertificate'] = $true
    $conn = New-Object System.Data.SqlClient.SqlConnection
    $conn.ConnectionString = $csb.ConnectionString
    $conn.Credential = $credential
    try {
        $conn.Open()
    } catch {
        $sqlException = $_.Exception
        while ($null -ne $sqlException -and $sqlException -isnot [System.Data.SqlClient.SqlException]) {
            $sqlException = $sqlException.InnerException
        }
        if ($null -ne $sqlException) {
            foreach ($sqlError in $sqlException.Errors) {
                Write-Host ("SQL ERROR: numero={0}; estado={1}; clase={2}" -f $sqlError.Number,$sqlError.State,$sqlError.Class)
            }
            throw "SQL Server no permitio la conexion. Servidor=$WoServer; base=$WoDatabase; usuario=$WoUser. Compare estos tres valores con una conexion nueva y exitosa en SSMS. El estado mostrado por el cliente puede ser generico; el registro del servidor contiene mas detalle."
        }
        throw 'No fue posible abrir la conexion SQL. No se modifico ningun dato.'
    }
    Write-Host 'Conexion SQL World Office: OK'

    $productsQuery = @'
SELECT IdInventario AS source_id, CodigoInventario AS code,
       Descripcion AS name, Clasificacion AS classification,
       Descripcion_Grupo_Uno AS category
FROM dbo.Vista_Tabla_Inventarios
WHERE Activo = -1
  AND CodigoInventario IS NOT NULL
  AND LTRIM(RTRIM(CodigoInventario)) <> ''
  AND (
    (Descripcion_Grupo_Uno = N'PRODUCTOS NO FABRICADOS POR LA EMPRESA' AND Clasificacion = N'Producto')
    OR (Descripcion_Grupo_Uno = N'SERVICIOS' AND Clasificacion = N'Servicio')
  )
ORDER BY IdInventario;
'@
    $employeesQuery = @'
SELECT IdTercero AS source_id, Identificacion AS identification,
       LTRIM(RTRIM(
         COALESCE(NULLIF(LTRIM(RTRIM(Primer_Nombre)), N'') + N' ', N'') +
         COALESCE(NULLIF(LTRIM(RTRIM(Segundo_Nombre)), N'') + N' ', N'') +
         COALESCE(NULLIF(LTRIM(RTRIM(Primer_Apellido)), N'') + N' ', N'') +
         COALESCE(NULLIF(LTRIM(RTRIM(Segundo_Apellido)), N''), N'')
       )) AS name
FROM dbo.Vista_Tabla_Terceros
WHERE CHARINDEX(N';Empleado;', N';' + COALESCE(Propiedades, N'') + N';') > 0
ORDER BY IdTercero;
'@
    $assetsQuery = @'
SELECT IdInventario AS source_id, CodigoInventario AS code,
       Descripcion AS name, Clasificacion AS classification,
       Descripcion_Grupo_Uno AS category,
       CAST(N'' AS nvarchar(128)) AS serial
FROM dbo.Vista_Tabla_Inventarios
WHERE Activo = -1
  AND Descripcion_Grupo_Uno = N'ACTIVOS FIJOS'
  AND Clasificacion IN (N'Activo Fijo', N'Producto')
ORDER BY IdInventario;
'@
    $datasets = @{}
    if ($Scope -in @('ProductsEmployees','Products','All')) {
        Write-Host 'Leyendo TODO el alcance de productos y servicios ...'
        $datasets.product = @(Read-SourceRows $productsQuery)
    }
    if ($Scope -in @('ProductsEmployees','Employees','All')) {
        Write-Host 'Leyendo TODOS los terceros marcados Empleado ...'
        $datasets.employee = @(Read-SourceRows $employeesQuery)
    }
    if ($Scope -in @('Assets','All')) {
        Write-Host 'Leyendo ACTIVOS FIJOS: Activo Fijo y Producto, un equipo por codigo ...'
        $datasets.asset = @(Read-SourceRows $assetsQuery)
    }
    $conn.Close()
    # Namespace estable por servidor y base. No reutiliza IDs de la demo.
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $sourceBytes = [Text.Encoding]::UTF8.GetBytes(($WoServer.ToLowerInvariant() + '|' + $WoDatabase.ToLowerInvariant()))
        $sourceHash = ([BitConverter]::ToString($sha.ComputeHash($sourceBytes))).Replace('-','').ToLowerInvariant()
    } finally { $sha.Dispose() }
    $sourceKey = 'wo_' + $sourceHash.Substring(0,24)
    Write-Host "Identificador de fuente: $sourceKey"
    $script:sourceRejected = 0
    $validated = @{}
    foreach ($kind in @('product','employee','asset')) {
        if (!$datasets.ContainsKey($kind)) { continue }
        if ($datasets[$kind].Count -eq 0) { throw "Consulta vacia para $kind. Verifique fuente antes de sincronizar." }
        $validated[$kind] = @(Get-ValidatedSource $kind $datasets[$kind])
        Write-Host "FUENTE | tipo=$kind | leidos=$($datasets[$kind].Count) | validos=$($validated[$kind].Count)"
        if ($validated[$kind].Count -eq 0) { throw "No hay registros validos para $kind." }
    }
    if ($Scope -eq 'ProductsEmployees') { Write-Host 'Activos excluidos por -Scope ProductsEmployees; use All para incluirlos.' }
    if ($Apply) {
        Write-Host 'Se crearan/actualizaran registros del alcance indicado. No se archivan registros.'
        Write-Host 'Los campos comerciales/fotos de productos se conservan. Nuevos productos usan valores predeterminados de Odoo para precios e impuestos.'
        Write-Host "Filas omitidas en validacion de origen: $script:sourceRejected"
        $confirm = Read-Host "Con respaldo reciente y simulacion revisada, escriba SINCRONIZAR-$CompanyId para aplicar en $($company[0].name)"
        if ($confirm -cne "SINCRONIZAR-$CompanyId") { throw 'Cancelado antes de escribir en Odoo.' }
    }
    $failed = 0
    foreach ($kind in @('product','employee','asset')) {
        if (!$validated.ContainsKey($kind)) { continue }
        $items = $validated[$kind]
        $totals = @{}
        for ($offset=0; $offset -lt $items.Count; $offset += $BatchSize) {
            $batch = New-Object System.Collections.ArrayList
            for ($index=$offset; $index -lt [Math]::Min($offset+$BatchSize,$items.Count); $index++) { [void]$batch.Add($items[$index]) }
            Write-Host ("Procesando {0}: {1}-{2} de {3} ..." -f $kind,($offset+1),($offset+$batch.Count),$items.Count)
            $answer = Invoke-Connector 'sync_batch' @{
                source_key=$sourceKey;company_id=$CompanyId;kind=$kind
                rows=@($batch.ToArray());dry_run=([bool](-not $Apply))
            }
            $results = @($answer.results)
            if ($results.Count -ne $batch.Count -or [bool]$answer.dry_run -ne (-not [bool]$Apply) -or [string]$answer.kind -ne $kind) {
                throw 'Respuesta de lote inesperada. Detenido; no repetir cambiando el identificador de fuente.'
            }
            foreach ($result in $results) {
                $status = [string]$result.status
                if (!$totals.ContainsKey($status)) { $totals[$status] = 0 }
                $totals[$status]++
                if ($status -eq 'error') {
                    $failed++
                    Write-Host "REVISAR | tipo=$kind | WO_ID=$($result.source_id) | motivo=$($result.error)"
                }
            }
        }
        Write-Host "RESUMEN $kind : $(($totals.GetEnumerator() | Sort-Object Name | ForEach-Object { $_.Key + '=' + $_.Value }) -join '; ')"
    }
    if ($Apply) { Write-Host 'EJECUCION TERMINADA. Los vinculos quedan guardados en la base Odoo.' }
    else { Write-Host 'SIMULACION TERMINADA. No se escribio ningun registro en Odoo ni en World Office.' }
    Write-Host "Omitidos_origen=$script:sourceRejected | Errores_destino=$failed"
    if ($failed -gt 0 -or $script:sourceRejected -gt 0) { throw 'Ejecucion con incidencias: revise los motivos. Los lotes aplicados permanecen guardados.' }
} finally {
    if ($null -ne $conn) { $conn.Dispose() }
    if ($null -ne $headers) { $headers.Clear() }
    $apiKey = $null
    $sqlPasswordText = $null
    if ($pointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
    if ($null -ne $secureKey) { $secureKey.Dispose() }
    if ($null -ne $sqlPassword) { $sqlPassword.Dispose() }
}
