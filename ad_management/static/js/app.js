// Jetson Ad Manager - Frontend JavaScript
class AdManager {
    constructor() {
        this.init();
    }

    init() {
        this.setupEventListeners();
        this.loadAds();
        this.loadStatus();
    }

    setupEventListeners() {
        // Upload area events
        const uploadArea = document.getElementById('uploadArea');
        const fileInput = document.getElementById('fileInput');

        uploadArea.addEventListener('click', () => fileInput.click());
        uploadArea.addEventListener('dragover', this.handleDragOver.bind(this));
        uploadArea.addEventListener('dragleave', this.handleDragLeave.bind(this));
        uploadArea.addEventListener('drop', this.handleDrop.bind(this));

        fileInput.addEventListener('change', this.handleFileSelect.bind(this));

        // Refresh button
        document.getElementById('refreshBtn').addEventListener('click', () => {
            this.loadAds();
            this.loadStatus();
        });

        // Modal events
        document.getElementById('closeModal').addEventListener('click', this.closeModal.bind(this));
        document.getElementById('previewModal').addEventListener('click', (e) => {
            if (e.target.id === 'previewModal') {
                this.closeModal();
            }
        });
    }

    handleDragOver(e) {
        e.preventDefault();
        document.getElementById('uploadArea').classList.add('dragover');
    }

    handleDragLeave(e) {
        e.preventDefault();
        document.getElementById('uploadArea').classList.remove('dragover');
    }

    handleDrop(e) {
        e.preventDefault();
        document.getElementById('uploadArea').classList.remove('dragover');
        const files = e.dataTransfer.files;
        this.uploadFiles(files);
    }

    handleFileSelect(e) {
        const files = e.target.files;
        this.uploadFiles(files);
    }

    async uploadFiles(files) {
        if (files.length === 0) return;

        const progressContainer = document.getElementById('uploadProgress');
        const progressFill = document.getElementById('progressFill');
        const progressText = document.getElementById('progressText');

        progressContainer.style.display = 'block';

        for (let i = 0; i < files.length; i++) {
            const file = files[i];
            
            // Update progress text
            progressText.textContent = `Uploading ${file.name} (${i + 1}/${files.length})...`;
            
            try {
                await this.uploadSingleFile(file, (progress) => {
                    progressFill.style.width = `${progress}%`;
                });
                
                this.showToast(`✅ ${file.name} uploaded successfully!`, 'success');
            } catch (error) {
                this.showToast(`❌ Failed to upload ${file.name}: ${error.message}`, 'error');
            }
        }

        // Hide progress and refresh
        progressContainer.style.display = 'none';
        progressFill.style.width = '0%';
        document.getElementById('fileInput').value = '';
        
        this.loadAds();
        this.loadStatus();
    }

    uploadSingleFile(file, progressCallback) {
        return new Promise((resolve, reject) => {
            const formData = new FormData();
            formData.append('file', file);

            const xhr = new XMLHttpRequest();

            xhr.upload.addEventListener('progress', (e) => {
                if (e.lengthComputable) {
                    const progress = (e.loaded / e.total) * 100;
                    progressCallback(progress);
                }
            });

            xhr.addEventListener('load', () => {
                if (xhr.status === 200) {
                    const response = JSON.parse(xhr.responseText);
                    if (response.success) {
                        resolve(response);
                    } else {
                        reject(new Error(response.error));
                    }
                } else {
                    const response = JSON.parse(xhr.responseText);
                    reject(new Error(response.error || 'Upload failed'));
                }
            });

            xhr.addEventListener('error', () => {
                reject(new Error('Network error'));
            });

            xhr.open('POST', '/api/upload');
            xhr.send(formData);
        });
    }

    async loadAds() {
        const loading = document.getElementById('loading');
        const adsGrid = document.getElementById('adsGrid');
        const emptyState = document.getElementById('emptyState');

        loading.style.display = 'block';
        adsGrid.innerHTML = '';
        emptyState.style.display = 'none';

        try {
            const response = await fetch('/api/ads');
            const data = await response.json();

            if (data.success) {
                if (data.ads.length === 0) {
                    emptyState.style.display = 'block';
                } else {
                    this.renderAds(data.ads);
                }
            } else {
                this.showToast(`❌ Failed to load ads: ${data.error}`, 'error');
            }
        } catch (error) {
            this.showToast(`❌ Network error: ${error.message}`, 'error');
        } finally {
            loading.style.display = 'none';
        }
    }

    renderAds(ads) {
        const adsGrid = document.getElementById('adsGrid');
        
        ads.forEach(ad => {
            const adCard = this.createAdCard(ad);
            adsGrid.appendChild(adCard);
        });
    }

    createAdCard(ad) {
        const card = document.createElement('div');
        card.className = 'ad-card';

        const preview = this.createPreview(ad);
        const typeIcon = ad.type === 'image' ? '🖼️' : '🎬';

        card.innerHTML = `
            <div class="ad-preview" onclick="adManager.previewAd('${ad.filename}', '${ad.type}')">
                ${preview}
            </div>
            <div class="ad-info">
                <div class="ad-filename">${ad.filename}</div>
                <div class="ad-meta">
                    <span class="ad-type">${typeIcon} ${ad.type}</span>
                    <span>${ad.size_mb} MB</span>
                </div>
                <div class="ad-meta">
                    <span>Modified: ${ad.modified}</span>
                </div>
                <div class="ad-actions">
                    <button class="btn btn-preview" onclick="adManager.previewAd('${ad.filename}', '${ad.type}')">
                        👁️ Preview
                    </button>
                    <button class="btn btn-delete" onclick="adManager.deleteAd('${ad.filename}')">
                        🗑️ Delete
                    </button>
                </div>
            </div>
        `;

        return card;
    }

    createPreview(ad) {
        if (ad.type === 'image') {
            return `<img src="${ad.url}" alt="${ad.filename}" loading="lazy">`;
        } else if (ad.type === 'video') {
            return `<video src="${ad.url}" muted></video>`;
        } else {
            return `<div class="file-icon">📄</div>`;
        }
    }

    previewAd(filename, type) {
        const modal = document.getElementById('previewModal');
        const modalBody = document.getElementById('modalBody');

        let content = '';
        if (type === 'image') {
            content = `<img src="/api/ads/file/${filename}" alt="${filename}">`;
        } else if (type === 'video') {
            content = `<video src="/api/ads/file/${filename}" controls autoplay muted>
                Your browser does not support the video tag.
            </video>`;
        }

        modalBody.innerHTML = content;
        modal.style.display = 'block';
    }

    closeModal() {
        const modal = document.getElementById('previewModal');
        const modalBody = document.getElementById('modalBody');
        
        modal.style.display = 'none';
        modalBody.innerHTML = '';
    }

    async deleteAd(filename) {
        if (!confirm(`Are you sure you want to delete "${filename}"?`)) {
            return;
        }

        try {
            const response = await fetch(`/api/ads/${filename}`, {
                method: 'DELETE'
            });

            const data = await response.json();

            if (data.success) {
                this.showToast(`✅ ${filename} deleted successfully!`, 'success');
                this.loadAds();
                this.loadStatus();
            } else {
                this.showToast(`❌ Failed to delete ${filename}: ${data.error}`, 'error');
            }
        } catch (error) {
            this.showToast(`❌ Network error: ${error.message}`, 'error');
        }
    }

    async loadStatus() {
        try {
            const response = await fetch('/api/status');
            const data = await response.json();

            if (data.success) {
                const statusText = document.getElementById('status-text');
                statusText.textContent = `${data.status.total_ads} ads • ${data.status.total_size_mb} MB`;
            }
        } catch (error) {
            console.error('Failed to load status:', error);
        }
    }

    showToast(message, type = 'success') {
        const container = document.getElementById('toastContainer');
        const toast = document.createElement('div');
        toast.className = `toast ${type}`;
        toast.textContent = message;

        container.appendChild(toast);

        // Auto remove after 5 seconds
        setTimeout(() => {
            if (toast.parentNode) {
                toast.parentNode.removeChild(toast);
            }
        }, 5000);
    }
}

// Initialize the app when DOM is loaded
document.addEventListener('DOMContentLoaded', () => {
    window.adManager = new AdManager();
});

// Handle keyboard shortcuts
document.addEventListener('keydown', (e) => {
    // Escape key to close modal
    if (e.key === 'Escape') {
        window.adManager.closeModal();
    }
    
    // Ctrl/Cmd + R to refresh
    if ((e.ctrlKey || e.metaKey) && e.key === 'r') {
        e.preventDefault();
        window.adManager.loadAds();
        window.adManager.loadStatus();
    }
});
