/**
 * cert-install.js — Platform detection and UI logic for the certificate
 * installation page.
 *
 * Detects the user's platform (iOS, macOS, Windows, Linux, Android),
 * highlights the matching card, and allows expanding/collapsing other cards.
 *
 * Also provides the banner injection function for use on other pages
 * (streams.html, login.html).
 */

(function () {
    'use strict';

    // -----------------------------------------------------------------------
    // Platform detection
    // -----------------------------------------------------------------------

    /**
     * Detect the user's platform from the User-Agent and platform hints.
     * Returns one of: 'ios', 'android', 'macos', 'windows', 'linux', 'unknown'
     */
    function detectPlatform() {
        const ua = navigator.userAgent || '';
        const platform = navigator.platform || '';

        // iOS: iPhone, iPad, iPod
        // Note: iPad on iOS 13+ reports as "MacIntel" — check touch support
        if (/iPhone|iPod/.test(ua)) {
            return 'ios';
        }
        if (/iPad/.test(ua)) {
            return 'ios';
        }
        // iPad with desktop UA (iOS 13+)
        if (/Macintosh/.test(ua) && 'ontouchend' in document) {
            return 'ios';
        }

        // Android
        if (/Android/.test(ua)) {
            return 'android';
        }

        // macOS (after iPad check to avoid false positive)
        if (/Macintosh|MacIntel|MacPPC/.test(ua) || /Mac/.test(platform)) {
            return 'macos';
        }

        // Windows
        if (/Windows|Win32|Win64/.test(ua) || /Win/.test(platform)) {
            return 'windows';
        }

        // Linux (after Android check)
        if (/Linux/.test(ua) || /Linux/.test(platform)) {
            return 'linux';
        }

        return 'unknown';
    }

    // -----------------------------------------------------------------------
    // Card interaction on the install page
    // -----------------------------------------------------------------------

    function initInstallPage() {
        const cardsContainer = document.getElementById('platformCards');
        if (!cardsContainer) return;

        const platform = detectPlatform();
        const cards = cardsContainer.querySelectorAll('.platform-card');

        // Highlight the detected platform's card
        cards.forEach(function (card) {
            const cardPlatform = card.getAttribute('data-platform');

            if (cardPlatform === platform) {
                card.classList.add('detected');
            }

            // Click header to expand/collapse (toggle for non-detected cards)
            var header = card.querySelector('.platform-card-header');
            if (header) {
                header.addEventListener('click', function () {
                    // If this is the detected card, don't collapse it
                    if (card.classList.contains('detected')) return;
                    card.classList.toggle('expanded');
                });
            }
        });

        // Inject a copy button on every command block. Operator directive
        // 2026-06-15: cert-install instructions need to be one-click
        // copy-friendly, especially the multi-line Chrome policy heredoc.
        decorateCodeBlocksWithCopyButtons(cardsContainer);
    }

    /**
     * Copy `text` to the clipboard. Returns a Promise that resolves on
     * success. Uses the modern Clipboard API when available (secure
     * contexts), falls back to a hidden textarea + execCommand for
     * http/older browsers — the install page is reachable over plain
     * HTTP during initial setup, where navigator.clipboard is null.
     */
    function copyToClipboard(text) {
        if (navigator.clipboard && window.isSecureContext) {
            return navigator.clipboard.writeText(text);
        }
        return new Promise(function (resolve, reject) {
            try {
                var ta = document.createElement('textarea');
                ta.value = text;
                ta.style.position = 'fixed';
                ta.style.opacity = '0';
                ta.style.pointerEvents = 'none';
                document.body.appendChild(ta);
                ta.focus();
                ta.select();
                var ok = document.execCommand('copy');
                document.body.removeChild(ta);
                ok ? resolve() : reject(new Error('execCommand returned false'));
            } catch (e) {
                reject(e);
            }
        });
    }

    /**
     * Walk every .step-code element under `root` and inject a Copy
     * button. The text source is the inner code/pre — whitespace is
     * preserved verbatim (important for the Chrome policy heredoc).
     *
     * Idempotent: if a .step-code already has a .copy-btn child, skips.
     */
    function decorateCodeBlocksWithCopyButtons(root) {
        var blocks = root.querySelectorAll('.step-code');
        blocks.forEach(function (block) {
            if (block.querySelector('.copy-btn')) return;

            // Source text: prefer <pre><code> inner, else <code> inner,
            // else the block's own textContent.
            var sourceEl = block.querySelector('pre > code')
                        || block.querySelector('code')
                        || block;

            // Make the container relative so the absolute-positioned
            // button anchors correctly.
            if (getComputedStyle(block).position === 'static') {
                block.style.position = 'relative';
            }

            var btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'copy-btn';
            btn.setAttribute('aria-label', 'Copy command to clipboard');
            btn.innerHTML = '<i class="far fa-copy"></i> <span class="copy-btn-label">Copy</span>';

            btn.addEventListener('click', function (e) {
                e.preventDefault();
                e.stopPropagation();
                var text = sourceEl.textContent.replace(/^\s+|\s+$/g, '');
                copyToClipboard(text).then(function () {
                    btn.classList.add('copied');
                    btn.querySelector('.copy-btn-label').textContent = 'Copied!';
                    setTimeout(function () {
                        btn.classList.remove('copied');
                        btn.querySelector('.copy-btn-label').textContent = 'Copy';
                    }, 1800);
                }).catch(function () {
                    btn.classList.add('copy-failed');
                    btn.querySelector('.copy-btn-label').textContent = 'Copy failed';
                    setTimeout(function () {
                        btn.classList.remove('copy-failed');
                        btn.querySelector('.copy-btn-label').textContent = 'Copy';
                    }, 2200);
                });
            });

            block.appendChild(btn);
        });
    }

    // -----------------------------------------------------------------------
    // Banner for other pages (streams, login)
    //
    // Usage: include this script on any page, then call:
    //   window.CertInstall.showBannerIfNeeded(containerSelector)
    //
    // The banner shows once per device. Dismissing sets a localStorage flag.
    // -----------------------------------------------------------------------

    var BANNER_DISMISSED_KEY = 'nvr_cert_banner_dismissed';

    /**
     * Inject the certificate install banner into the given container element.
     * Does nothing if:
     *   - The user previously dismissed the banner (localStorage)
     *   - The page is served over HTTP (no cert issue)
     *   - The CA cert is not available (no point showing it)
     *
     * @param {string} containerSelector  CSS selector for the element to
     *                                    prepend the banner into.
     */
    function showBannerIfNeeded(containerSelector) {
        // Don't show if already dismissed
        if (localStorage.getItem(BANNER_DISMISSED_KEY) === 'true') {
            return;
        }

        // Only relevant on HTTPS
        if (location.protocol !== 'https:') {
            return;
        }

        // Check if CA is available before showing banner
        fetch('/api/cert/status')
            .then(function (resp) { return resp.json(); })
            .then(function (data) {
                if (!data.ca_available) return;

                var container = document.querySelector(containerSelector);
                if (!container) return;

                var banner = document.createElement('div');
                banner.className = 'cert-banner';
                banner.innerHTML =
                    '<i class="fas fa-shield-alt"></i>' +
                    '<span>Seeing security warnings? ' +
                    '<a href="/install-cert">Install the NVR certificate</a>' +
                    ' to trust this connection permanently.</span>' +
                    '<button class="cert-banner-dismiss" title="Dismiss">&times;</button>';

                // Dismiss handler
                banner.querySelector('.cert-banner-dismiss').addEventListener('click', function (e) {
                    e.preventDefault();
                    banner.remove();
                    localStorage.setItem(BANNER_DISMISSED_KEY, 'true');
                });

                // Insert after the container element
                container.parentNode.insertBefore(banner, container.nextSibling);
            })
            .catch(function () {
                // Silently fail — banner is non-critical
            });
    }

    // -----------------------------------------------------------------------
    // Initialize
    // -----------------------------------------------------------------------

    // If we're on the install page, set up card interactions
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initInstallPage);
    } else {
        initInstallPage();
    }

    // Expose banner function globally
    window.CertInstall = {
        showBannerIfNeeded: showBannerIfNeeded,
        detectPlatform: detectPlatform
    };

})();
