package com.meshvision

import android.app.Application
import com.ffalcon.mercury.android.sdk.MercurySDK

/**
 * MeshVision Application — initialises the Mercury SDK for RayNeo X3 glasses.
 */
class MeshVisionApp : Application() {
    override fun onCreate() {
        super.onCreate()
        MercurySDK.init(this)
    }
}
