$container = "bluesky-crossposter"
$baseDir = "T:\bluesky-crossposter"

# Function to clean up the bytecode
function Clean-PyCache {
    param ([string]$Path)
    Write-Host "Cleaning up __pycache__ folders..." -ForegroundColor Gray
    Get-ChildItem -Path $Path -Filter "__pycache__" -Recurse -Directory | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
}

# The clean array - no trailing slashes
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
    "cookies.json"
)

if (docker ps -q -f name=$container) {
    foreach ($item in $syncItems) {
        # Source is the item in the container root
        $sourcePath = "${container}:/${item}"
        
        # DESTINATION IS THE KEY: We target the ROOT of your T: drive project.
        # This forces a merge of the folder names rather than a nested copy.
        $destPath = $baseDir 

        Write-Host "Pulling $item..." -ForegroundColor Cyan
        docker cp $sourcePath $destPath
    }

    Clean-PyCache -Path $baseDir
    Write-Host "Pull Complete!" -ForegroundColor Green
} else {
    Write-Error "Container $container is not running."
}