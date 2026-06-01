#include <led-drivers/other/DriverOtherGroup.h>
#include <led-drivers/LedDeviceManufactory.h>

#include <QJsonArray>

#include <linalg.h>

DriverOtherGroup::DriverOtherGroup(const QJsonObject& deviceConfig)
	: LedDevice(deviceConfig)
{
}

DriverOtherGroup::~DriverOtherGroup()
{
	teardownChildren();
}

void DriverOtherGroup::teardownChildren()
{
	for (auto* child : _children)
	{
		if (child != nullptr)
		{
			child->stop();		// disables -> switchOff -> close (synchronous, same thread)
			delete child;
		}
	}
	_children.clear();
}

bool DriverOtherGroup::init(QJsonObject deviceConfig)
{
	// Base init parses colorOrder/intervals and, importantly, sets _ledCount from the injected
	// "currentLedCount" -- which we forward to each child so they expect this instance's LED layout.
	bool initOK = LedDevice::init(deviceConfig);

	// Re-init (reconfigure): drop any previously built children first.
	teardownChildren();

	// The group does no anti-flickering itself; each child applies its own on its output path.
	_antiFlickeringFilter = false;

	const QJsonArray devices = deviceConfig["devices"].toArray();
	if (devices.isEmpty())
	{
		Error(_log, "Group device has no child 'devices' configured");
		return false;
	}

	for (const QJsonValue& entry : devices)
	{
		QJsonObject childConfig = entry.toObject();

		// Children share this instance's LED count + smoothing timing.
		childConfig["currentLedCount"] = static_cast<int>(_ledCount);
		childConfig["smoothingRefreshTime"] = deviceConfig["smoothingRefreshTime"];
		childConfig["smoothingAntiFlickeringFilter"] = deviceConfig["smoothingAntiFlickeringFilter"];

		const QString childType = childConfig["type"].toString("UNSPECIFIED");

		LedDevice* child = hyperhdr::leds::CONSTRUCT_LED_DEVICE(childConfig);
		child->setParent(this);				// same thread + automatic cleanup
		child->setActiveDeviceType(childType);
		child->start();						// init + enable + open the child synchronously

		Info(_log, "Group: started child device '{:s}'", (childType));
		_children.push_back(child);
	}

	Info(_log, "Group device initialised with {:d} child device(s)", static_cast<int>(_children.size()));
	return initOK;
}

int DriverOtherGroup::open()
{
	// The group has no hardware of its own; (re)enable the children and mark ready.
	for (auto* child : _children)
		if (child != nullptr)
			child->enable();

	_isDeviceReady = true;
	return 0;
}

int DriverOtherGroup::close()
{
	for (auto* child : _children)
		if (child != nullptr)
			child->disable();

	_isDeviceReady = false;
	return 0;
}

std::pair<bool, int> DriverOtherGroup::writeInfiniteColors(SharedOutputColors nonlinearRgbColors)
{
	if (nonlinearRgbColors == nullptr || nonlinearRgbColors->empty())
		return { true, 0 };

	// Forward a PRIVATE copy of this frame to every child, so a child's blink/terminate fill cannot
	// mutate the shared buffer the other children (and this device) still reference. Each child does
	// its own colorOrder + anti-flickering on its own output path.
	auto frame = std::make_shared<std::vector<linalg::aliases::float3>>(*nonlinearRgbColors);

	for (auto* child : _children)
		if (child != nullptr)
			child->handleSignalFinalOutputColorsReady(frame);

	// Report "handled" so the base write() does not also run a finite conversion at the group level.
	return { true, static_cast<int>(nonlinearRgbColors->size()) };
}

LedDevice* DriverOtherGroup::construct(const QJsonObject& deviceConfig)
{
	return new DriverOtherGroup(deviceConfig);
}

bool DriverOtherGroup::isRegistered = hyperhdr::leds::REGISTER_LED_DEVICE("group", "leds_group_4_special", DriverOtherGroup::construct);
