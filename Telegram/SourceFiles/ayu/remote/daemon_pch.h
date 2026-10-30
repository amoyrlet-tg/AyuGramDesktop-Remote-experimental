#pragma once

#include <QtCore/QtCore>
#include <QtNetwork/QtNetwork>
#include <QtGui/QColor>
#include <algorithm>
#include <atomic>
#include <deque>
#include <map>
#include <memory>
#include <optional>
#include <set>
#include <vector>
#include <range/v3/all.hpp>
#include <gsl/gsl>
#include <rpl/rpl.h>
#include <crl/crl.h>
#include "base/assertion.h"
#include "base/basic_types.h"
#include "base/flat_map.h"
#include "base/flat_set.h"
#include "base/invoke_queued.h"
#include "base/weak_ptr.h"
#include "base/weak_qptr.h"
#include "scheme.h"
#include "mtproto/facade.h"
#include "mtproto/type_utils.h"
#include "logs.h"

inline constexpr auto ApiId = TDESKTOP_API_ID;
