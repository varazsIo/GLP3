import type React from 'react';

/**
 * Assigns a node to a callback or object ref, so a component can hold its own
 * ref to an element and forward it too.
 */
export function assignRef<E>(ref: React.Ref<E> | undefined, node: E | null): void {
  if (typeof ref === 'function') {
    ref(node);
  } else if (ref && typeof ref === 'object') {
    (ref as React.MutableRefObject<E | null>).current = node;
  }
}

export default assignRef;
