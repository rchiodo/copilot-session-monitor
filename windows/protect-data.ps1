param([Parameter(Mandatory = $true)][string]$DataDirectory)
$ErrorActionPreference = 'Stop'
$acl = [System.Security.AccessControl.DirectorySecurity]::new()
$acl.SetAccessRuleProtection($true, $false)
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$system = [System.Security.Principal.SecurityIdentifier]::new('S-1-5-18')
$acl.SetOwner($user)
foreach ($identity in @($user, $system)) {
    $rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
        $identity, 'FullControl', 'ContainerInherit, ObjectInherit', 'None', 'Allow')
    $acl.AddAccessRule($rule)
}
[System.IO.Directory]::SetAccessControl($DataDirectory, $acl)
