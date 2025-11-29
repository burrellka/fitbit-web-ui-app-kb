
# Deploy Script for Fitbit Wellness App
# Usage: .\deploy.ps1 [-Clean]

param (
    [switch]$Clean
)

$ImageName = "fitbit-wellness-enhanced:latest"
$RepoImage = "brain40/fitbit-wellness-enhanced:latest"

Write-Host "🚀 Starting Deployment Process..." -ForegroundColor Cyan

# 1. Build
if ($Clean) {
    Write-Host "🧹 Clean build requested (no cache)..." -ForegroundColor Yellow
    docker-compose build --no-cache
} else {
    Write-Host "⚡ Fast build (using cache)..." -ForegroundColor Green
    docker-compose build
}

if ($LASTEXITCODE -ne 0) {
    Write-Host "❌ Build failed!" -ForegroundColor Red
    exit 1
}

# 2. Tag
Write-Host "🏷️ Tagging image..." -ForegroundColor Cyan
docker tag $ImageName $RepoImage

# 3. Push
Write-Host "⬆️ Pushing to Docker Hub ($RepoImage)..." -ForegroundColor Cyan
docker push $RepoImage

if ($LASTEXITCODE -ne 0) {
    Write-Host "❌ Push failed!" -ForegroundColor Red
    exit 1
}

Write-Host "✅ Deployment Complete!" -ForegroundColor Green
Write-Host "Now pull and restart on your home server:" -ForegroundColor Gray
Write-Host "docker-compose pull && docker-compose up -d" -ForegroundColor White
