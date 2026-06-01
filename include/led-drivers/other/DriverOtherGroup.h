#pragma once

#include <led-drivers/LedDevice.h>

#ifndef PCH_ENABLED
	#include <vector>
#endif

/// @brief "group" LED device: a meta-device that drives MULTIPLE real LED devices from a
/// SINGLE instance, forwarding each child the same color frame.
///
/// This lets one full-screen color (e.g. the "unicolor_mean" or "advanced_ambient" mapping)
/// light two or more physical devices without running a second HyperHDR instance: one layout,
/// one set of processing/smoothing/calibration, identical output to every child.
///
/// Config: { "type": "group", "devices": [ { <child device config> }, ... ] }. Each child config
/// is a normal device config (its own "type", "host", "colorOrder", ...). The group injects this
/// instance's "currentLedCount" and smoothing settings into each child, then constructs and starts
/// it via the standard device factory. Children live in this device's thread.
class DriverOtherGroup : public LedDevice
{
	Q_OBJECT

public:
	explicit DriverOtherGroup(const QJsonObject& deviceConfig);
	~DriverOtherGroup() override;

	/// Factory entry registered with REGISTER_LED_DEVICE.
	static LedDevice* construct(const QJsonObject& deviceConfig);

protected:
	bool init(QJsonObject deviceConfig) override;
	int open() override;
	int close() override;
	std::pair<bool, int> writeInfiniteColors(SharedOutputColors nonlinearRgbColors) override;

private:
	void teardownChildren();

	std::vector<LedDevice*> _children;
	static bool isRegistered;
};
