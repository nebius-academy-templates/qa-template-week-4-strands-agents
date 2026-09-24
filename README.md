# QA workflow with Strands

This package adds the `strands-workflow/` Python application to the course's
Kotlin API and Appium practice project. It also supplies the mobile step-capture
helper needed by the workflow's review packet.

## Install

1. Install the [QA workflow skills package](https://github.com/nebius-academy-templates/qa-template-week-4-workflow)
   in the project first.
2. Download this repository using **Code → Download ZIP** and open
   `repository-files/`.
3. Copy `repository-files/strands-workflow/` into the project root.
4. Copy `repository-files/appium-tests/src/test/kotlin/rule/AppiumTestCase.kt`
   to the same path in the project. This supplied helper adds UI page source
   alongside screenshots after successful Allure steps. If you customized that
   helper, merge its `attachScreenState()` changes into your copy and retain the
   successful-step call; preserve your other changes.
5. Follow the [runtime guide](repository-files/strands-workflow/README.md) for
   prerequisites, Python setup, commands, options, and reports. After copying,
   this guide is available at `strands-workflow/README.md` in the project.

The fresh starter deliberately leaves the review agent disconnected in
`workflow.py`. Connecting that route remains the course exercise.

## Update an existing installation

If you already completed the review-route exercise, keep your edited
`strands-workflow/workflow.py` when updating the other runtime files. In its
`run_workflow()` task text, change `Complete the API automation workflow` to
`Complete the test automation workflow`; retain your review node, edges and
freshness check. Apply the `AppiumTestCase.kt` capture update from step 4 and
install the current Python requirements. This update does not include any case
implementations; keep the tests and plans already in your practice project.
