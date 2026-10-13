# Discovery authoring pairs

| Weak | Strong | Why |
| --- | --- | --- |
| Search should be fast and reliable. | When a cached index is available, searching a known identifier displays its matching record within 300 ms. | One observable outcome with a bound. |
| Run the unit suite to prove missing indexes work. | When the index is absent, a search displays “Index unavailable” and returns no stale records. | Error behavior, independent of verification machinery. |
| Every capability is a must-have. | Offline lookup is required for the field visit; export may wait because the visit can finish without it. | Priority follows the product consequence. |
| We decided to support every file type. | File types remain unexamined; the stated assumption is no additional formats until the owner confirms scope. | An inference is not a human decision. |
