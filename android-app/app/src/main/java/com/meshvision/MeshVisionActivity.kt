package com.meshvision

import android.graphics.Color
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.webkit.JavascriptInterface
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.repeatOnLifecycle
import com.ffalcon.mercury.android.sdk.touch.TempleAction
import com.ffalcon.mercury.android.sdk.ui.activity.BaseMirrorActivity
import com.meshvision.databinding.ActivityMeshVisionBinding
import kotlinx.coroutines.launch

/**
 * MeshVision AR HUD — 640×480 binocular WebView overlay for RayNeo X3 Pro.
 *
 * Loads the mesh-vision HUD from assets and connects via WebSocket to the
 * MeshVision backend running on the paired MacBook.
 *
 * Temple gestures:
 *   Click          → cycle HUD layers (1:RAD → 2:SPE → 3:TOP → 4:ALL)
 *   SlideForward   → zoom in (topology)
 *   SlideBackward  → zoom out (topology)
 *   DoubleClick    → exit app
 *   TripleClick    → exit app
 */
class MeshVisionActivity : BaseMirrorActivity<ActivityMeshVisionBinding>() {

    companion object {
        private const val TAG = "MeshVision"
        private const val PREFS_NAME = "MeshVisionPrefs"
        private const val KEY_BACKEND_URL = "backend_url"
        // Default: the Mac's typical local IP on the shared WiFi network.
        // Override via SharedPreferences or the JS bridge.
        private const val DEFAULT_BACKEND_URL = "10.0.10.178:8420"
        private const val HUD_ASSET = "file:///android_asset/hud/glasses.html"
    }

    private var currentLayer = 3  // 0=radar, 1=spectrum, 2=topology, 3=all

    // ─────────────────────── Lifecycle ───────────────────────

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setupWebView()
        loadHud()
        collectTempleActions()
    }

    override fun onDestroy() {
        mBindingPair.updateView {
            hudWebView.stopLoading()
            hudWebView.destroy()
        }
        super.onDestroy()
    }

    @Deprecated("Use onBackPressedDispatcher", ReplaceWith("onBackPressedDispatcher"))
    @Suppress("DEPRECATION")
    override fun onBackPressed() {
        Log.i(TAG, "onBackPressed → exitApp")
        exitApp()
    }

    // ─────────────────────── WebView setup ───────────────────────

    private fun setupWebView() {
        mBindingPair.updateView {
            hudWebView.setBackgroundColor(Color.BLACK)
            hudWebView.settings.apply {
                javaScriptEnabled = true
                domStorageEnabled = true
                mixedContentMode = WebSettings.MIXED_CONTENT_ALWAYS_ALLOW
                useWideViewPort = true
                loadWithOverviewMode = true
                cacheMode = WebSettings.LOAD_NO_CACHE
                allowFileAccess = true
                allowContentAccess = true
                // Performance: hardware acceleration is on by default
                mediaPlaybackRequiresUserGesture = false
            }
            hudWebView.addJavascriptInterface(MeshBridge(), "MeshBridge")
            hudWebView.webViewClient = object : WebViewClient() {
                override fun onPageFinished(view: WebView?, url: String?) {
                    super.onPageFinished(view, url)
                    Log.d(TAG, "HUD page loaded: $url")
                    // Inject the backend URL so the HUD knows where to connect
                    injectBackendUrl()
                }
            }
        }
    }

    private fun loadHud() {
        mBindingPair.updateView {
            hudWebView.loadUrl(HUD_ASSET)
        }
        Log.d(TAG, "loadHud: loading $HUD_ASSET")
    }

    /**
     * After the HUD HTML loads, inject the backend URL into localStorage
     * and trigger a reconnect so the WebSocket points at the Mac backend.
     */
    private fun injectBackendUrl() {
        val backendUrl = getBackendUrl()
        val js = """
            (function() {
                window.MESH_BACKEND_URL = '$backendUrl';
                localStorage.setItem('mesh_backend_url', '$backendUrl');
                if (typeof reconnectWS === 'function') reconnectWS();
            })();
        """.trimIndent()
        evalJs(js)
        Log.d(TAG, "Injected backend URL: $backendUrl")
    }

    // ─────────────────────── Preferences ───────────────────────

    private fun getBackendUrl(): String {
        val prefs = getSharedPreferences(PREFS_NAME, MODE_PRIVATE)
        return prefs.getString(KEY_BACKEND_URL, DEFAULT_BACKEND_URL) ?: DEFAULT_BACKEND_URL
    }

    private fun setBackendUrl(url: String) {
        getSharedPreferences(PREFS_NAME, MODE_PRIVATE)
            .edit()
            .putString(KEY_BACKEND_URL, url)
            .apply()
    }

    // ─────────────────────── Temple gestures ───────────────────────

    private fun collectTempleActions() {
        lifecycleScope.launch {
            repeatOnLifecycle(Lifecycle.State.RESUMED) {
                templeActionViewModel.state.collect { action ->
                    when (action) {
                        is TempleAction.Click -> {
                            // Cycle layers: 1 → 2 → 3 → 4(all) → 1
                            currentLayer = (currentLayer + 1) % 4
                            val layerKey = when (currentLayer) {
                                0 -> "1"  // radar
                                1 -> "2"  // spectrum
                                2 -> "3"  // topology
                                3 -> "4"  // all
                                else -> "4"
                            }
                            evalJs("document.dispatchEvent(new KeyboardEvent('keydown',{key:'$layerKey'}))")
                            Log.d(TAG, "Temple Click → layer $layerKey")
                        }
                        is TempleAction.DoubleClick -> {
                            Log.i(TAG, "DoubleClick → exitApp")
                            exitApp()
                        }
                        is TempleAction.TripleClick -> {
                            Log.i(TAG, "TripleClick → exitApp")
                            exitApp()
                        }
                        is TempleAction.SlideForward -> {
                            // Zoom in on topology
                            evalJs("if(typeof S!=='undefined'){S.topoZoom=Math.min(3,S.topoZoom*1.2)}")
                            Log.d(TAG, "Temple SlideForward → zoom in")
                        }
                        is TempleAction.SlideBackward -> {
                            // Zoom out on topology
                            evalJs("if(typeof S!=='undefined'){S.topoZoom=Math.max(0.3,S.topoZoom*0.8)}")
                            Log.d(TAG, "Temple SlideBackward → zoom out")
                        }
                        is TempleAction.SlideContinuous -> {
                            // Continuous slide: fine zoom
                            val delta = action.delta
                            if (delta > 0) {
                                evalJs("if(typeof S!=='undefined'){S.topoZoom=Math.min(3,S.topoZoom*1.05)}")
                            } else {
                                evalJs("if(typeof S!=='undefined'){S.topoZoom=Math.max(0.3,S.topoZoom*0.95)}")
                            }
                        }
                        else -> Unit
                    }
                }
            }
        }
    }

    // ─────────────────────── Exit ───────────────────────

    private fun exitApp() {
        Log.i(TAG, "exitApp() — finishing, killing process")
        mBindingPair.updateView {
            hudWebView.stopLoading()
            hudWebView.destroy()
        }
        finishAffinity()
        Handler(Looper.getMainLooper()).postDelayed({
            android.os.Process.killProcess(android.os.Process.myPid())
        }, 200)
    }

    // ─────────────────────── JS helpers ───────────────────────

    private fun evalJs(script: String) {
        mBindingPair.updateView {
            hudWebView.evaluateJavascript(script, null)
        }
    }

    // ─────────────────────── JS Bridge ───────────────────────

    /**
     * JavaScript interface exposed as `window.MeshBridge` to the WebView.
     * Allows the HUD to read/write backend URL preferences.
     */
    inner class MeshBridge {

        @JavascriptInterface
        fun setBackendUrl(url: String) {
            Log.d(TAG, "MeshBridge.setBackendUrl: $url")
            this@MeshVisionActivity.setBackendUrl(url)
        }

        @JavascriptInterface
        fun getBackendUrl(): String {
            val url = this@MeshVisionActivity.getBackendUrl()
            Log.d(TAG, "MeshBridge.getBackendUrl: $url")
            return url
        }

        @JavascriptInterface
        fun exitApp() {
            Log.i(TAG, "MeshBridge.exitApp() called from JS")
            Handler(Looper.getMainLooper()).post {
                this@MeshVisionActivity.exitApp()
            }
        }
    }
}
