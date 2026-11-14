$container = "bluesky-crossposter"
$baseDir = "T:\bluesky-crossposter"

# Clean array - no trailing slashes
$syncItems = @(
    "docker-compose.yml",
    "input",
    "main",
    "output",
    "run.py",
    "run.sh",
    "settings",
    "Dockerfile",
    "env.example",
    "README.md",
    "requirements.txt",
    "crossposter.js",
    "db\mentions.json"
)

if (docker ps -q -f name=$container) {
    foreach ($item in $syncItems) {
        # Source is the specific file or folder on your T: drive
        $sourcePath = Join-Path $baseDir $item
        
        # DESTINATION: top-level items go to the container's root directory, which triggers a merge.
        # Items inside a subfolder (like db\mentions.json) go to that same subfolder in the container.
        $parent = (Split-Path $item -Parent) -replace '\\', '/'
        $destPath = if ($parent) { "${container}:/${parent}/" } else { "${container}:/" }

        if (Test-Path $sourcePath) {
            Write-Host "Pushing $item to container..." -ForegroundColor Yellow
            docker cp $sourcePath $destPath
        } else {
            Write-Warning "Source not found: $sourcePath"
        }
    }
    
    Write-Host "`nRestarting container to apply changes..." -ForegroundColor Magenta
    docker restart $container
    
    Write-Host "Deploy Complete! Structure should be flat." -ForegroundColor Green
} else {
    Write-Error "Container $container is not running. Start it with 'docker compose up -d' first."
}