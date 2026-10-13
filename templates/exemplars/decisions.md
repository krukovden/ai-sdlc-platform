# Decision authoring pairs

| Weak | Strong | Why |
| --- | --- | --- |
| Use one transaction because separate writes are worse. | Commit the result and its checkpoint atomically; separate writes can leave a checkpoint claiming a result that was never saved after a crash. | Names the rejected option and broken invariant. |
| Call load(), then validate(), then save(). | The persistence boundary accepts only validated records and commits all changes atomically. | States an architectural contract without prescribing method names or call order. |
| The choice is reversible. | Replacing the storage format requires converting retained records and validating checkpoint continuity before switching readers. | States the actual reversal cost. |
