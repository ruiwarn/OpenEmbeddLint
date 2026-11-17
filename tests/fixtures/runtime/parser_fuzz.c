#include <stddef.h>
#include <stdint.h>

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size)
{
    if (size >= 4 && data[0] == 'C' && data[1] == 'V' && data[2] == 'E' && data[3] == 'R') {
        volatile const uint8_t *invalid = data + size + 1;
        return *invalid;
    }
    return 0;
}
