package rule

import actions.DrawerActions
import actions.DriverActions
import actions.MapActions
import actions.NotificationActions
import actions.OnboardingActions
import actions.OrderActions
import actions.RegionActions
import actions.SupportActions
import io.appium.java_client.android.AndroidDriver
import io.qameta.allure.Allure
import org.junit.jupiter.api.AfterEach
import org.junit.jupiter.api.BeforeEach
import org.junit.jupiter.api.TestInfo
import org.junit.jupiter.api.extension.ExtendWith
import org.openqa.selenium.OutputType
import java.io.ByteArrayInputStream

/**
 * Base for e2e tests written as a sequence of ACTIONS.
 *
 *   class MyTest : AppiumTestCase() {
 *       @Test fun flow() {
 *           onboarding.completeUntilMap()
 *           drawer.openOrders()
 *           orders.assertHistoryPrices(...)
 *       }
 *   }
 *
 * Design notes:
 * - Everything above the driver is stateless: pages are catalogs of Element
 *   values, actions are singleton objects, and both resolve the current
 *   thread's driver through DriverFactory per operation. This class owns
 *   ONLY the session lifecycle and the step() reporter; the vocabulary
 *   fields below are plain references kept for test readability.
 * - Layering: pages (`pages/`) = element catalogs, NO asserts; actions =
 *   steps + checks (asserts live here); testcases = the action sequence.
 * - Waits and retries live on Element (waitFor / waitForGone / retryClick);
 *   device-level primitives live on Device.
 * - Failure artifacts (logcat + screenshot + page source) come from
 *   ArtifactsOnFailure; FailureDigest condenses them into one small
 *   build/reports/digests/test_log_{method}_{try}.txt per failure. The artifact
 *   exception handler runs before the digest's after-test-execution callback,
 *   allowing the digest to reuse the captured logcat.
 */
@ExtendWith(FailureDigest::class, ArtifactsOnFailure::class)
abstract class AppiumTestCase {
    lateinit var driver: AndroidDriver
        private set

    /**
     * Whether the session launches the app already authorized (the boolean
     * launch extra "authenticated" makes NavHost start at the map). Default
     * true: most suites test post-auth screens and should not pay the
     * onboarding cost per test. Override to false when onboarding itself is
     * the subject (OnboardingSmokeTest).
     */
    protected open val startAuthorized: Boolean = true

    // The vocabulary an e2e test is written in.
    val onboarding = OnboardingActions
    val map = MapActions
    val drawer = DrawerActions
    val orders = OrderActions
    val notifications = NotificationActions
    val region = RegionActions
    val support = SupportActions

    // Named driverSignup, not driver: `driver` is already the AndroidDriver field.
    val driverSignup = DriverActions

    @BeforeEach
    fun startSession(testInfo: TestInfo) {
        driver = DriverFactory.createDriver(startAuthorized)
        DriverFactory.currentDeviceUdid()?.let { udid ->
            val testName =
                "${testInfo.testClass.orElse(null)?.simpleName ?: "UnknownTest"} > " +
                    testInfo.displayName
            Allure.label("device", udid)
            DeviceExecutionEvidence.record(udid, testName)
        }
        // Reset after the app is installed and the session exists. A reset
        // performed only by the suite runner can miss a clean device, while a
        // teardown-only reset cannot recover from an aborted previous run.
        ConditionControl.reset()
        recordPreconditions()
    }

    @AfterEach
    fun endSession() {
        // Keep teardown hygiene too, so a state enabled mid-test is cleared
        // immediately instead of waiting for the next session.
        try {
            runCatching { ConditionControl.reset() }
            if (::driver.isInitialized) driver.quit()
        } finally {
            DriverFactory.releaseCurrentDevice()
        }
    }

    /**
     * Wraps [body] as a named Allure step and attaches a screenshot and UI page
     * source after a successful step. Captures are independent and do not change
     * the test outcome. [ArtifactsOnFailure] captures evidence when the step throws.
     */
    protected fun step(
        name: String,
        body: () -> Unit,
    ) = Allure.step(
        name,
        Allure.ThrowableRunnableVoid {
            body()
            attachScreenState()
        },
    )

    private fun recordPreconditions() {
        val appStart =
            if (startAuthorized) {
                "The app starts authorized on the ride form."
            } else {
                "The app starts unauthenticated on Phone login."
            }
        val preconditions =
            """
            ## Preconditions

            - The local fake-api and Appium server are running.
            - The app and backend sandbox session are reset; all controlled failure states are disabled.
            - $appStart
            """.trimIndent()

        Allure.description(preconditions)
    }

    private fun attachScreenState() {
        val activeDriver = DriverFactory.current() ?: return
        runCatching {
            val screenshot = activeDriver.getScreenshotAs(OutputType.BYTES)
            Allure.addAttachment(
                "Screen after step",
                "image/png",
                ByteArrayInputStream(screenshot),
                ".png",
            )
        }.onFailure { failure ->
            runCatching {
                Allure.addAttachment("Screen capture failed", failure.stackTraceToString())
            }
        }
        runCatching {
            val pageSource = activeDriver.pageSource.orEmpty().toByteArray(Charsets.UTF_8)
            Allure.addAttachment(
                "UI page source",
                "application/xml",
                ByteArrayInputStream(pageSource),
                ".xml",
            )
        }.onFailure { failure ->
            runCatching {
                Allure.addAttachment("UI page source capture failed", failure.stackTraceToString())
            }
        }
    }
}
