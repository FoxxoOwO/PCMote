package com.pcmote.network

import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.os.Handler
import android.os.Looper
import android.util.Log
import java.util.ArrayDeque

/**
 * Thin wrapper around [NsdManager] that discovers `_pcmote._tcp.` services in the local network.
 *
 * Android only allows a single active resolve operation per [NsdManager], therefore resolutions
 * are queued and executed one after another.
 */
class NsdDiscovery(context: Context) {

    data class Server(val name: String, val host: String, val port: Int) {
        val id: String get() = "$name@$host:$port"
    }

    private companion object {
        const val TAG = "NsdDiscovery"
        const val SERVICE_TYPE = "_pcmote._tcp."
    }

    private val nsdManager: NsdManager? =
        context.applicationContext.getSystemService(Context.NSD_SERVICE) as? NsdManager

    private val mainHandler = Handler(Looper.getMainLooper())

    private var discoveryListener: NsdManager.DiscoveryListener? = null
    private val resolveQueue = ArrayDeque<NsdServiceInfo>()
    private var resolving = false

    private var onFound: ((Server) -> Unit)? = null
    private var onLost: ((String) -> Unit)? = null
    private var onError: ((String) -> Unit)? = null

    fun start(
        onFound: (Server) -> Unit,
        onLost: (String) -> Unit,
        onError: (String) -> Unit
    ) {
        val manager = nsdManager
        if (manager == null) {
            onError("NSD is not available on this device.")
            return
        }
        if (discoveryListener != null) return

        this.onFound = onFound
        this.onLost = onLost
        this.onError = onError

        val listener = object : NsdManager.DiscoveryListener {
            override fun onDiscoveryStarted(serviceType: String) {
                Log.i(TAG, "Discovery started for $serviceType")
            }

            override fun onServiceFound(serviceInfo: NsdServiceInfo) {
                Log.i(TAG, "Service found: ${serviceInfo.serviceName}")
                enqueueResolve(serviceInfo)
            }

            override fun onServiceLost(serviceInfo: NsdServiceInfo) {
                Log.i(TAG, "Service lost: ${serviceInfo.serviceName}")
                mainHandler.post { this@NsdDiscovery.onLost?.invoke(serviceInfo.serviceName) }
            }

            override fun onDiscoveryStopped(serviceType: String) {
                Log.i(TAG, "Discovery stopped for $serviceType")
            }

            override fun onStartDiscoveryFailed(serviceType: String, errorCode: Int) {
                Log.w(TAG, "Start discovery failed: $errorCode")
                stopInternal()
                mainHandler.post { this@NsdDiscovery.onError?.invoke("Discovery failed (code $errorCode).") }
            }

            override fun onStopDiscoveryFailed(serviceType: String, errorCode: Int) {
                Log.w(TAG, "Stop discovery failed: $errorCode")
                stopInternal()
            }
        }

        discoveryListener = listener
        try {
            manager.discoverServices(SERVICE_TYPE, NsdManager.PROTOCOL_DNS_SD, listener)
        } catch (t: Throwable) {
            Log.e(TAG, "discoverServices failed", t)
            discoveryListener = null
            onError("Discovery could not be started: ${t.message}")
        }
    }

    fun stop() {
        stopInternal()
    }

    private fun stopInternal() {
        val manager = nsdManager
        val listener = discoveryListener
        discoveryListener = null
        resolveQueue.clear()
        resolving = false
        if (manager == null || listener == null) return
        try {
            manager.stopServiceDiscovery(listener)
        } catch (t: Throwable) {
            Log.w(TAG, "stopServiceDiscovery failed", t)
        }
    }

    private fun enqueueResolve(serviceInfo: NsdServiceInfo) {
        resolveQueue.addLast(serviceInfo)
        resolveNext()
    }

    private fun resolveNext() {
        val manager = nsdManager ?: return
        if (resolving) return
        val next = resolveQueue.pollFirst() ?: return
        resolving = true

        try {
            manager.resolveService(next, object : NsdManager.ResolveListener {
                override fun onResolveFailed(serviceInfo: NsdServiceInfo, errorCode: Int) {
                    Log.w(TAG, "Resolve failed for ${serviceInfo.serviceName}: $errorCode")
                    resolving = false
                    resolveNext()
                }

                override fun onServiceResolved(serviceInfo: NsdServiceInfo) {
                    val host = serviceInfo.host?.hostAddress
                    val port = serviceInfo.port
                    resolving = false
                    if (host.isNullOrBlank() || port <= 0) {
                        Log.w(TAG, "Resolved service without usable address: $serviceInfo")
                        resolveNext()
                        return
                    }
                    val server = Server(serviceInfo.serviceName, host, port)
                    Log.i(TAG, "Resolved: ${server.id}")
                    mainHandler.post { onFound?.invoke(server) }
                    resolveNext()
                }
            })
        } catch (t: Throwable) {
            Log.e(TAG, "resolveService failed", t)
            resolving = false
            mainHandler.post { onError?.invoke("Address resolution failed: ${t.message}") }
        }
    }
}
