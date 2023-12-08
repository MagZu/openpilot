#pragma once

#include <QFrame>
#include <QHBoxLayout>
#include <QLabel>
#include <QPushButton>
#include <QVBoxLayout>
#include <QProcess>

#ifndef QCOM
#include "selfdrive/ui/qt/network/networking.h"
#endif
#include "selfdrive/ui/qt/offroad/settings.h"
#include "selfdrive/ui/qt/widgets/input.h"
#include "selfdrive/ui/qt/widgets/toggle.h"
#include "selfdrive/ui/qt/widgets/offroad_alerts.h"
#include "selfdrive/ui/qt/widgets/scrollview.h"
#include "selfdrive/ui/qt/widgets/controls.h"
#include "selfdrive/ui/qt/widgets/ssh_keys.h"
#include "common/params.h"
#include "common/util.h"
#include "system/hardware/hw.h"
#include "selfdrive/ui/qt/home.h"


class TinklaTogglesPanel : public ListWidget {
  Q_OBJECT
public:
  explicit TinklaTogglesPanel(SettingsWindow *parent = nullptr);
};

class TeslaPreApTogglesPanel : public ListWidget {
  Q_OBJECT
public:
  explicit TeslaPreApTogglesPanel(SettingsWindow *parent = nullptr);
};

class TeslaTogglesPanel : public ListWidget {
  Q_OBJECT
public:
  explicit TeslaTogglesPanel(SettingsWindow *parent = nullptr);
};
