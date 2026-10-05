param([Parameter(Mandatory = $true)][string]$DataDirectory,
      [Parameter(Mandatory = $true)][string]$BindAddress)
$ErrorActionPreference = 'Stop'
$rsa = [System.Security.Cryptography.RSA]::Create(2048)
try {
    $request = [System.Security.Cryptography.X509Certificates.CertificateRequest]::new(
        'CN=Local Copilot Monitor', $rsa, [System.Security.Cryptography.HashAlgorithmName]::SHA256,
        [System.Security.Cryptography.RSASignaturePadding]::Pkcs1)
    $san = [System.Security.Cryptography.X509Certificates.SubjectAlternativeNameBuilder]::new()
    $san.AddIpAddress([System.Net.IPAddress]::Parse('127.0.0.1'))
    if ($BindAddress -ne '127.0.0.1') { $san.AddIpAddress([System.Net.IPAddress]::Parse($BindAddress)) }
    $request.CertificateExtensions.Add($san.Build())
    $request.CertificateExtensions.Add(
        [System.Security.Cryptography.X509Certificates.X509BasicConstraintsExtension]::new($false, $false, 0, $true))
    $usage = [System.Security.Cryptography.OidCollection]::new()
    [void]$usage.Add([System.Security.Cryptography.Oid]::new('1.3.6.1.5.5.7.3.1'))
    $request.CertificateExtensions.Add(
        [System.Security.Cryptography.X509Certificates.X509EnhancedKeyUsageExtension]::new($usage, $true))
    $certificate = $request.CreateSelfSigned([DateTimeOffset]::UtcNow.AddDays(-1), [DateTimeOffset]::UtcNow.AddYears(2))
    try {
        [IO.File]::WriteAllBytes((Join-Path $DataDirectory 'collector.pfx'),
            $certificate.Export([System.Security.Cryptography.X509Certificates.X509ContentType]::Pfx, ''))
        $base64 = [Convert]::ToBase64String($certificate.RawData, [Base64FormattingOptions]::InsertLineBreaks)
        [IO.File]::WriteAllText((Join-Path $DataDirectory 'collector-cert.pem'),
            "-----BEGIN CERTIFICATE-----`n$base64`n-----END CERTIFICATE-----`n")
    } finally { $certificate.Dispose() }
} finally { $rsa.Dispose() }
