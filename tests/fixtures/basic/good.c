int classify(int value) {
    if (value < 0) {
        return -1;
    }
    if (value == 0) {
        return 0;
    }
    return 1;
}

int main(int argc, char** argv) {
    (void)argv;
    return classify(argc - 1);
}
